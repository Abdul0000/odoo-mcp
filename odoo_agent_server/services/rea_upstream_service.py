from __future__ import annotations
import ast
from pathlib import Path
from typing import Any
from .core_finder_service import CoreFinderService

class ReaUpstreamService:
    """Compare custom model extensions with installed dependency source."""
    _MAX_FILE_BYTES = 256000

    @staticmethod
    def _strings(node):
        if node is None: return []
        items = node.elts if isinstance(node, (ast.List, ast.Tuple, ast.Set)) else [node]
        return [x.value for x in items if isinstance(x, ast.Constant) and isinstance(x.value, str)]

    @staticmethod
    def _assign(cls, name):
        for item in cls.body:
            if isinstance(item, (ast.Assign, ast.AnnAssign)):
                targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                if any(isinstance(t, ast.Name) and t.id == name for t in targets): return item.value
        return None

    @staticmethod
    def _sig(node):
        a=node.args; pos=[x.arg for x in [*a.posonlyargs,*a.args]]
        if pos and pos[0] in {"self","cls"}: pos.pop(0)
        return {"positional":pos,"keyword_only":[x.arg for x in a.kwonlyargs],
                "vararg":a.vararg.arg if a.vararg else None,"kwarg":a.kwarg.arg if a.kwarg else None}

    @staticmethod
    def _field(node):
        if not isinstance(node,ast.Call): return None,None
        fn=node.func; typ=fn.id if isinstance(fn,ast.Name) else fn.attr if isinstance(fn,ast.Attribute) else None
        comodel=node.args[0].value if node.args and isinstance(node.args[0],ast.Constant) and isinstance(node.args[0].value,str) else None
        for kw in node.keywords:
            if kw.arg in {"comodel_name","comodel"} and isinstance(kw.value,ast.Constant) and isinstance(kw.value.value,str): comodel=kw.value.value
        return typ,comodel

    @classmethod
    def _models(cls, source, path):
        try: tree=ast.parse(source,filename=path)
        except SyntaxError as exc: return [],[{"code":"PYTHON_PARSE_ERROR","path":path,"message":str(exc)[:160]}]
        result=[]
        for node in ast.walk(tree):
            if not isinstance(node,ast.ClassDef): continue
            own=cls._strings(cls._assign(node,"_name")); inh=cls._strings(cls._assign(node,"_inherit"))
            names=list(dict.fromkeys([*own,*inh]))
            if not names: continue
            methods={m.name:cls._sig(m) for m in node.body if isinstance(m,(ast.FunctionDef,ast.AsyncFunctionDef))}
            fields={}
            for stmt in node.body:
                if isinstance(stmt,(ast.Assign,ast.AnnAssign)):
                    targets=stmt.targets if isinstance(stmt,ast.Assign) else [stmt.target]; spec=cls._field(stmt.value)
                    if spec[0]:
                        for target in targets:
                            if isinstance(target,ast.Name): fields[target.id]=spec
            for name in names: result.append({"model":name,"defined":name in own,"methods":methods,"fields":fields,"path":path,"line":node.lineno})
        return result,[]

    def compare(self, *, module_path:Path, module_name:str, client_name:str|None=None,
                max_dependency_files:int=30, max_total_chars:int=12000)->dict[str,Any]:
        module_path=module_path.resolve(strict=True); manifest_path=module_path/"__manifest__.py"
        if not module_path.is_dir() or not manifest_path.is_file(): raise ValueError("Valid addon directory and manifest required")
        manifest=ast.literal_eval(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest,dict): raise ValueError("Manifest must contain a dictionary")
        deps=[x for x in manifest.get("depends",[]) if isinstance(x,str) and x.strip()]
        roots=CoreFinderService()._resolve_roots(None,client_name=client_name)
        root_labels=[{"priority":str(p),"path":str(Path(root))} for p,root in roots]
        upstream={}; scanned=[]; omitted=[]; warnings=[]; chars=0
        budget=max(4000,min(int(max_total_chars),20000)); limit=max(5,min(int(max_dependency_files),60)); stop=False
        for _,root in roots:
            rp=Path(root).resolve()
            for dep_name in deps:
                dep=CoreFinderService._find_module_dir(rp,dep_name)
                if not dep: continue
                for folder in ("models","wizard","wizards","controllers"):
                    folder_path=dep/folder
                    if not folder_path.is_dir(): continue
                    for path in sorted(folder_path.rglob("*.py")):
                        try:
                            resolved=path.resolve(strict=True); resolved.relative_to(dep.resolve()); size=resolved.stat().st_size
                        except (OSError,ValueError):
                            warnings.append({"code":"UNSAFE_OR_UNREADABLE_UPSTREAM_PATH","path":str(path)}); continue
                        rel=resolved.relative_to(rp).as_posix()
                        if size>self._MAX_FILE_BYTES: omitted.append(rel); continue
                        if len(scanned)>=limit or chars>=budget: omitted.append(rel); stop=True; break
                        source=resolved.read_text(encoding="utf-8",errors="replace")
                        if chars+len(source)>budget: omitted.append(rel); continue
                        chars+=len(source); scanned.append(rel); models,errs=self._models(source,rel); warnings.extend(errs)
                        for model in models: upstream.setdefault(model["model"],model)
                    if stop: break
                if stop: break
            if stop: break
        custom=[]; custom_count=0
        for path in sorted(module_path.rglob("*.py")):
            try:
                resolved=path.resolve(strict=True); relpath=resolved.relative_to(module_path)
                if any(x in {".git","__pycache__","static","tests"} for x in relpath.parts) or resolved.stat().st_size>self._MAX_FILE_BYTES: continue
                source=resolved.read_text(encoding="utf-8",errors="replace")
            except (OSError,ValueError): continue
            custom_count+=1; models,errs=self._models(source,relpath.as_posix()); custom.extend(models); warnings.extend(errs)
        findings=[]
        for item in custom:
            base=upstream.get(item["model"])
            if not base:
                if not item["defined"]: findings.append({"code":"UPSTREAM_MODEL_NOT_FOUND","severity":"warning","model":item["model"],"message":"No matching model definition found in declared dependency source roots.","evidence":{"custom_path":item["path"],"custom_line":item["line"]}})
                continue
            for name,sig in item["methods"].items():
                old=base["methods"].get(name)
                if old and old!=sig: findings.append({"code":"METHOD_SIGNATURE_DRIFT","severity":"warning","model":item["model"],"symbol":name,"message":"Custom override signature differs from installed upstream definition.","custom_signature":sig,"upstream_signature":old,"evidence":{"custom_path":item["path"],"custom_line":item["line"],"upstream_path":base["path"],"upstream_line":base["line"]}})
                elif old is None and name not in {"__init__","__str__","__repr__"}: findings.append({"code":"CUSTOM_METHOD_NOT_FOUND_UPSTREAM","severity":"info","model":item["model"],"symbol":name,"message":"No same-named method exists upstream; confirm intentional extension or stale override.","evidence":{"custom_path":item["path"],"custom_line":item["line"],"upstream_path":base["path"],"upstream_line":base["line"]}})
            for name,spec in item["fields"].items():
                old=base["fields"].get(name)
                if old and old!=spec: findings.append({"code":"FIELD_TYPE_DRIFT","severity":"warning","model":item["model"],"symbol":name,"message":"Custom field declaration differs from upstream type or comodel.","custom_field":{"type":spec[0],"comodel":spec[1]},"upstream_field":{"type":old[0],"comodel":old[1]},"evidence":{"custom_path":item["path"],"custom_line":item["line"],"upstream_path":base["path"],"upstream_line":base["line"]}})
        return {"status":"compared","module":module_name,"dependencies":deps,"upstream_roots":root_labels,
            "coverage":{"dependency_files_scanned":len(scanned),"dependency_files_omitted":len(omitted),"custom_files_scanned":custom_count,"source_chars":chars,"complete":not omitted},
            "findings":findings[:200],"warnings":warnings[:100],"omitted_files":omitted[:100],
            "limitations":["Only installed source roots and declared dependencies are compared.","Version/edition is not inferred without source-tree metadata.","Static comparisons do not prove runtime compatibility."]}
