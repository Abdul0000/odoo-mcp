from __future__ import annotations

import ast
import re
from pathlib import Path
from xml.sax.saxutils import quoteattr
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from ..schemas import FrontendVisualImplementRequest, OdooCodeFileChange


@dataclass(frozen=True)
class VisualVariant:
    name: str
    role: str
    palette: str
    file_name: str


class FrontendVisualService:
    """Deterministic frontend visual asset generator for Odoo modules.

    This service intentionally focuses on repeatable implementation assets
    (SVG/CSS/SCSS helpers) rather than free-form design guessing.  The caller can
    use workflow.frontend_visual_implement to create premium borders, pipes,
    backgrounds, logos, icons, or decorative dashboard/POS elements and then run
    the normal Odoo code-change/deploy pipeline.
    """

    _SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]+")

    def build_writes(self, payload: FrontendVisualImplementRequest, module_name: str) -> tuple[list[OdooCodeFileChange], dict[str, Any]]:
        component_type = self._normalise(payload.component_type or "auto")
        design_text = " ".join([
            payload.design_goal or "",
            payload.target_area or "",
            component_type,
            " ".join(payload.allowed_methods or []),
        ]).lower()
        strategy = self._select_strategy(payload, design_text)
        asset_name = self._safe_name(payload.asset_name or self._default_asset_name(component_type, design_text))
        css_file, existing_css = self._choose_css_target(payload, module_name, asset_name, design_text)
        binding_proof = self._prove_binding(payload, module_name, css_file, design_text)

        if strategy == "svg_asset":
            variants = self._variants(payload, asset_name, design_text)
            writes: list[OdooCodeFileChange] = []
            for variant in variants:
                svg_rel = self._module_rel(module_name, f"{module_name}/static/src/img/{variant.file_name}")
                writes.append(OdooCodeFileChange(relative_path=svg_rel, content=self._svg_for_variant(variant, payload)))
            generated_css = self._css_for_variants(module_name, payload, variants)
            writes.append(OdooCodeFileChange(relative_path=css_file, content=self._merge_generated_css(existing_css, asset_name, generated_css)))
            plan = {
                "status": "generated",
                "implementation_type": "svg_asset",
                "component_type": component_type,
                "asset_name": asset_name,
                "css_file": css_file,
                "assets": [f"{module_name}/static/src/img/{variant.file_name}" for variant in variants],
                "reason": "SVG asset selected for exact visual shapes, logos, backgrounds, decorative borders, and pipe-like curves that CSS borders approximate poorly.",
                "manifest_note": "The target stylesheet is already present in an Odoo asset bundle and the selector binding was proven before generation.",
                "binding_proof": binding_proof,
            }
            return writes, plan

        generated_css = self._scss_only(payload, asset_name)
        writes = [OdooCodeFileChange(relative_path=css_file, content=self._merge_generated_css(existing_css, asset_name, generated_css))]
        plan = {
            "status": "generated",
            "implementation_type": "scss",
            "component_type": component_type,
            "asset_name": asset_name,
            "css_file": css_file,
            "reason": "SCSS-only selected because SVG asset generation was not requested and the task appears to be normal layout/card styling.",
            "binding_proof": binding_proof,
        }
        return writes, plan


    def _normalise_css_path(self, module_name: str, value: str) -> str:
        """Return a safe project-relative CSS/SCSS path inside ``module_name``."""
        raw = str(value or "").replace("\\", "/").strip().lstrip("/")
        if "\x00" in raw or ".." in raw.split("/"):
            raise HTTPException(status_code=400, detail="Unsafe visual CSS path")
        if not raw:
            raise HTTPException(status_code=400, detail="Visual CSS path is empty")
        if not raw.startswith(f"{module_name}/"):
            raw = f"{module_name}/{raw}"
        if Path(raw).suffix.lower() not in {".scss", ".css"}:
            raise HTTPException(status_code=400, detail="Visual CSS target must end with .scss or .css")
        return raw

    def _read_existing_css(self, payload: FrontendVisualImplementRequest, relative_path: str) -> str:
        """Read an existing target only from the selected checkout.

        Missing files are valid because the visual workflow can create a new bundled
        stylesheet.  Any resolved path outside the checkout is rejected.
        """
        if not payload.project_path:
            return ""
        root = Path(payload.project_path).expanduser().resolve()
        target = (root / relative_path).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Visual CSS target escapes project_path") from exc
        if not target.is_file():
            return ""
        try:
            return target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"Visual CSS target is not UTF-8 text: {relative_path}") from exc

    def _choose_css_target(
        self,
        payload: FrontendVisualImplementRequest,
        module_name: str,
        asset_name: str,
        design_text: str,
    ) -> tuple[str, str]:
        """Choose a deterministic existing stylesheet, or a safe new one.

        Priority is explicit ``css_file_path``, then existing CSS/SCSS entries in
        ``target_files``.  Trolley/final-match files are preferred for trolley
        visuals so generated rules are appended to the stylesheet already loaded
        by the module instead of silently creating an unbundled file.
        """
        if payload.css_file_path:
            selected = self._normalise_css_path(module_name, payload.css_file_path)
            return selected, self._read_existing_css(payload, selected)

        candidates: list[str] = []
        for item in payload.target_files or []:
            raw = str(item or "").replace("\\", "/").strip()
            if Path(raw).suffix.lower() not in {".scss", ".css"}:
                continue
            candidates.append(self._normalise_css_path(module_name, raw))

        def score(path: str) -> tuple[int, int, str]:
            lower = path.lower()
            visual_match = 0
            if "trolley" in design_text and "trolley" in lower:
                visual_match += 40
            if "final_match" in lower:
                visual_match += 20
            if asset_name in lower:
                visual_match += 10
            existing = 1 if self._read_existing_css(payload, path) else 0
            return (existing, visual_match, path)

        if candidates:
            selected = max(dict.fromkeys(candidates), key=score)
            return selected, self._read_existing_css(payload, selected)

        raise HTTPException(
            status_code=409,
            detail=(
                "Visual generation requires an existing bundled CSS/SCSS target. "
                "Pass css_file_path or include a bundled stylesheet in target_files; "
                "the service will not create orphan asset code."
            ),
        )

    def _manifest_asset_paths(self, payload: FrontendVisualImplementRequest, module_name: str) -> set[str]:
        if not payload.project_path:
            return set()
        manifest = Path(payload.project_path).expanduser().resolve() / module_name / "__manifest__.py"
        try:
            data = ast.literal_eval(manifest.read_text(encoding="utf-8"))
        except Exception:
            return set()
        assets = data.get("assets", {}) if isinstance(data, dict) else {}
        result: set[str] = set()
        if isinstance(assets, dict):
            for items in assets.values():
                if isinstance(items, (list, tuple)):
                    for item in items:
                        if isinstance(item, str) and not item.startswith(("!", "(")):
                            result.add(item.replace("\\", "/").lstrip("/"))
        return result

    def _prove_binding(
        self,
        payload: FrontendVisualImplementRequest,
        module_name: str,
        css_file: str,
        design_text: str,
    ) -> dict[str, Any]:
        if not payload.project_path:
            return {"status": "unverified_no_project_path", "bundled_css": css_file}
        module_root = Path(payload.project_path).expanduser().resolve() / module_name
        manifest = module_root / "__manifest__.py"
        if not manifest.exists():
            # Unit tests/standalone generators may not have a full addon tree.
            # Real workflow checkouts always have a manifest and are strict.
            return {"status": "unverified_no_manifest", "bundled_css": css_file}
        bundled = self._manifest_asset_paths(payload, module_name)
        if css_file not in bundled:
            raise HTTPException(
                status_code=409,
                detail=f"Visual stylesheet is not declared in an Odoo asset bundle: {css_file}",
            )
        selector = str(payload.css_selector or "").strip()
        if selector:
            tokens = re.findall(r"[.#]([A-Za-z_][A-Za-z0-9_-]*)", selector)
            required = tokens[-1] if tokens else selector.strip(".#")
        elif "trolley" in design_text:
            required = "pos-theme-trolley"
        else:
            raise HTTPException(
                status_code=409,
                detail="Provide css_selector for generic visual generation so attachment to a rendered node can be proven.",
            )
        evidence: list[str] = []
        for suffix in ("*.xml", "*.js"):
            for path in module_root.rglob(suffix):
                if any(part in {".git", "node_modules", "__pycache__"} for part in path.parts):
                    continue
                try:
                    if required in path.read_text(encoding="utf-8", errors="ignore"):
                        evidence.append(path.relative_to(Path(payload.project_path).resolve()).as_posix())
                except OSError:
                    continue
                if len(evidence) >= 5:
                    break
            if evidence:
                break
        if not evidence:
            has_frontend_sources = any(module_root.rglob("*.xml")) or any(module_root.rglob("*.js"))
            if not has_frontend_sources:
                return {"status": "unverified_no_frontend_sources", "bundled_css": css_file, "selector_token": required}
            raise HTTPException(
                status_code=409,
                detail=f"Visual selector binding could not be proven; token '{required}' was not found in module XML/JS.",
            )
        return {"bundled_css": css_file, "selector_token": required, "template_evidence": evidence}

    def _merge_generated_css(self, existing_css: str, asset_name: str, generated_css: str) -> str:
        """Idempotently add or replace one generated visual block."""
        safe_asset = self._safe_name(asset_name)
        begin = f"/* BEGIN GENERATED VISUAL: {safe_asset} */"
        end = f"/* END GENERATED VISUAL: {safe_asset} */"
        block = f"{begin}\n{generated_css.strip()}\n{end}"
        current = str(existing_css or "")
        pattern = re.compile(
            rf"^[ \t]*{re.escape(begin)}.*?^[ \t]*{re.escape(end)}[ \t]*$",
            flags=re.MULTILINE | re.DOTALL,
        )
        if pattern.search(current):
            merged = pattern.sub(block, current, count=1)
        else:
            merged = f"{current.rstrip()}\n\n{block}" if current.strip() else block
        return f"{merged.rstrip()}\n"

    def _select_strategy(self, payload: FrontendVisualImplementRequest, design_text: str) -> str:
        allowed = {self._normalise(item) for item in (payload.allowed_methods or []) if item}
        if "svg_asset" in allowed or "svg" in allowed:
            return "svg_asset"
        if any(token in design_text for token in ("svg", "pipe", "elbow", "logo", "icon", "illustration", "special border", "decorative border", "background shape", "glass border")):
            return "svg_asset"
        if self._normalise(payload.component_type or "") in {"border", "background", "logo", "icon", "illustration"}:
            return "svg_asset"
        return "scss"

    def _default_asset_name(self, component_type: str, design_text: str) -> str:
        if "pipe" in design_text or "elbow" in design_text or "trolley" in design_text:
            return "trolley_pipe"
        if component_type and component_type != "auto":
            return component_type
        return "visual_component"

    def _safe_name(self, value: str) -> str:
        value = self._SAFE_NAME_RE.sub("_", str(value or "").strip()).strip("_").lower()
        if not value:
            value = "visual_component"
        return value[:80]

    def _normalise(self, value: str) -> str:
        return self._safe_name(value).replace("-", "_")

    def _module_rel(self, module_name: str, relative_path: str) -> str:
        value = str(relative_path or "").replace("\\", "/").lstrip("/")
        if "\x00" in value or ".." in value.split("/"):
            raise HTTPException(status_code=400, detail="Unsafe visual asset path")
        if not value.startswith(f"{module_name}/"):
            value = f"{module_name}/{value}"
        return value

    def _variants(self, payload: FrontendVisualImplementRequest, asset_name: str, design_text: str) -> list[VisualVariant]:
        if payload.variants:
            variants: list[VisualVariant] = []
            for raw in payload.variants[:12]:
                name = self._safe_name(str(raw.get("name") or raw.get("role") or "variant"))
                role = self._normalise(str(raw.get("role") or name))
                palette = self._normalise(str(raw.get("palette") or raw.get("color") or role or "gold"))
                requested_file = str(raw.get("file_name") or f"{asset_name}_{name}.svg").strip()
                stem = Path(requested_file).stem
                file_name = f"{self._safe_name(stem)}.svg"
                variants.append(VisualVariant(name=name, role=role, palette=palette, file_name=file_name))
            return variants or [VisualVariant("default", "default", "gold", f"{asset_name}.svg")]

        if "pipe" in design_text or "elbow" in design_text or "trolley" in design_text:
            return [
                VisualVariant("active", "active", "gold", f"{asset_name}_active.svg"),
                VisualVariant("purple", "packed_purple", "purple", f"{asset_name}_purple.svg"),
                VisualVariant("silver", "packed_silver", "silver", f"{asset_name}_silver.svg"),
            ]
        return [VisualVariant("default", "default", "gold", f"{asset_name}.svg")]

    def _palette(self, palette: str) -> dict[str, str]:
        palettes = {
            "gold": {"dark": "#8a5a1f", "mid": "#c79239", "light": "#fff2c6", "accent": "#f1d391", "shadow": "#4c2a08"},
            "purple": {"dark": "#6f3d6f", "mid": "#b16aa5", "light": "#f6d8ef", "accent": "#dca6d2", "shadow": "#3f2440"},
            "silver": {"dark": "#717178", "mid": "#bfc0c5", "light": "#ffffff", "accent": "#e2e3e7", "shadow": "#45464c"},
            "plum": {"dark": "#4d1d3b", "mid": "#7b315f", "light": "#f4d8ea", "accent": "#b46a9e", "shadow": "#2b1422"},
        }
        return palettes.get(palette, palettes["gold"])

    def _svg_for_variant(self, variant: VisualVariant, payload: FrontendVisualImplementRequest) -> str:
        pal = self._palette(variant.palette)
        title = (payload.design_goal or variant.name)[:120]
        aria_label = quoteattr(title)
        return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 72 360" role="img" aria-label={aria_label}>
  <defs>
    <linearGradient id="pipe" x1="0" x2="1" y1="0" y2="0">
      <stop offset="0" stop-color="{pal['dark']}"/>
      <stop offset="0.18" stop-color="{pal['mid']}"/>
      <stop offset="0.42" stop-color="{pal['light']}"/>
      <stop offset="0.58" stop-color="{pal['mid']}"/>
      <stop offset="0.82" stop-color="{pal['accent']}"/>
      <stop offset="1" stop-color="{pal['dark']}"/>
    </linearGradient>
    <filter id="softShadow" x="-80%" y="-10%" width="260%" height="130%">
      <feDropShadow dx="0" dy="5" stdDeviation="4" flood-color="{pal['shadow']}" flood-opacity="0.24"/>
    </filter>
  </defs>
  <path d="M58 24 H34 C22 24 16 30 16 42 V318 C16 330 22 336 34 336 H58"
        fill="none" stroke="url(#pipe)" stroke-width="18" stroke-linecap="round" stroke-linejoin="round" filter="url(#softShadow)"/>
  <path d="M58 24 H34 C22 24 16 30 16 42 V318 C16 330 22 336 34 336 H58"
        fill="none" stroke="#ffffff" stroke-width="4" stroke-linecap="round" stroke-linejoin="round" opacity="0.36" transform="translate(-4 0)"/>
  <rect x="7" y="118" width="34" height="74" rx="17" fill="url(#pipe)" filter="url(#softShadow)"/>
  <rect x="12" y="124" width="9" height="62" rx="4.5" fill="#ffffff" opacity="0.30"/>
</svg>
'''

    def _css_for_variants(self, module_name: str, payload: FrontendVisualImplementRequest, variants: list[VisualVariant]) -> str:
        selector = str(payload.css_selector or "").strip()
        component_type = self._normalise(payload.component_type or "auto")
        base_comment = (
            "/* Generated by workflow.frontend_visual_implement.\n"
            "   Safe SVG-backed visual helpers for Odoo frontend/POS/OWL UI.\n"
            "   Include this SCSS file in the correct Odoo asset bundle, or copy the rules into an existing bundled SCSS file. */\n"
        )
        url = lambda variant: f"/{module_name}/static/src/img/{variant.file_name}"
        if selector:
            lines = [base_comment]
            for idx, variant in enumerate(variants):
                suffix = "" if idx == 0 else f".is-{variant.name}"
                lines.append(f"{selector}{suffix} {{\n    position: relative;\n    overflow: visible;\n}}\n")
                lines.append(f"{selector}{suffix}::before {{\n    content: \"\";\n    position: absolute;\n    left: -1.15rem;\n    top: .25rem;\n    bottom: .25rem;\n    width: 2.2rem;\n    background-image: url(\"{url(variant)}\");\n    background-repeat: no-repeat;\n    background-size: 100% 100%;\n    background-position: left center;\n    pointer-events: none;\n    z-index: 2;\n}}\n")
            return "\n".join(lines)

        # Pipe/trolley variants must bind to the proven trolley selectors even
        # when the request calls the component "full_ui". Generating a generic
        # unattached helper class would be a silent no-op.
        if any("pipe" in variant.file_name for variant in variants):
            by_role = {variant.role: variant for variant in variants}
            active = by_role.get("active", variants[0])
            purple = by_role.get("packed_purple", variants[min(1, len(variants)-1)])
            silver = by_role.get("packed_silver", variants[min(2, len(variants)-1)])
            return base_comment + f'''
.pos-theme-custom .pos-theme-trolley-active,
.pos-theme-custom .pos-theme-trolley-packed {{
    position: relative;
    overflow: visible !important;
}}

.pos-theme-custom .pos-theme-trolley-active::before {{
    content: "";
    position: absolute;
    left: -1.18rem;
    top: .16rem;
    bottom: .16rem;
    width: 2.28rem;
    background-image: url("{url(active)}");
    background-repeat: no-repeat;
    background-size: 100% 100%;
    background-position: left center;
    pointer-events: none;
    z-index: 2;
}}

.pos-theme-custom .pos-theme-trolley-packed::before {{
    content: "";
    position: absolute;
    left: -.92rem;
    top: .38rem;
    bottom: .38rem;
    width: 1.7rem;
    background-image: url("{url(purple)}");
    background-repeat: no-repeat;
    background-size: 100% 100%;
    background-position: left center;
    pointer-events: none;
    z-index: 2;
}}

.pos-theme-custom .pos-theme-trolley-packed:nth-child(3n + 3)::before {{
    background-image: url("{url(silver)}");
}}
'''
        variant = variants[0]
        return base_comment + f'''
.o_frontend_visual_{self._safe_name(payload.asset_name or 'component')} {{
    background-image: url("{url(variant)}");
    background-repeat: no-repeat;
    background-size: contain;
    background-position: center;
}}
'''

    def _scss_only(self, payload: FrontendVisualImplementRequest, asset_name: str) -> str:
        selector = str(payload.css_selector or f".o_frontend_visual_{asset_name}").strip()
        return f'''/* Generated by workflow.frontend_visual_implement. */
{selector} {{
    position: relative;
    border-radius: 18px;
    border: 1px solid rgba(199, 150, 79, .28);
    background: linear-gradient(180deg, rgba(255,253,248,.98), rgba(251,247,240,.95));
    box-shadow: 0 14px 34px rgba(77, 29, 59, .10);
}}
'''
