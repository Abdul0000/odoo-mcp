from pathlib import Path

from odoo_agent_server.schemas import VerifySeverity
from odoo_agent_server.services.repo_index_service import RepoIndexService
from odoo_agent_server.services.syntax_verifier_service import (
    SyntaxVerifierService,
    _template_declared_in_content,
)


def test_js_impact_adds_only_matching_template_xml(tmp_path):
    module = tmp_path / "sample_module"
    (module / "static" / "src" / "js").mkdir(parents=True)
    (module / "static" / "src" / "xml").mkdir(parents=True)
    (module / "views").mkdir()
    (module / "__manifest__.py").write_text("{'name': 'Sample'}", encoding="utf-8")

    js_file = module / "static" / "src" / "js" / "component.js"
    js_file.write_text(
        '/** @odoo-module **/\nclass X {}\nX.template = "sample_module.Component";\n',
        encoding="utf-8",
    )
    matching = module / "static" / "src" / "xml" / "component.xml"
    matching.write_text('<templates><t t-name="sample_module.Component" owl="1"/></templates>', encoding="utf-8")
    unrelated = module / "views" / "legacy.xml"
    unrelated.write_text('<odoo><template id="legacy"><t t-call="missing.layout"/></template></odoo>', encoding="utf-8")

    files = RepoIndexService().related_files([str(js_file)], project_path=str(tmp_path), max_related=20)

    assert str(js_file) in files
    assert str(module / "__manifest__.py") in files
    assert str(matching) in files
    assert str(unrelated) not in files


def test_server_qweb_external_ids_are_recognized():
    assert _template_declared_in_content(
        "website.layout",
        '<odoo><template id="layout" name="Website Layout"/></odoo>',
    )
    assert _template_declared_in_content(
        "payment.form",
        '<odoo><record id="form" model="ir.ui.view"><field name="arch" type="xml"/></record></odoo>',
    )
    assert not _template_declared_in_content(
        "website.layout",
        '<odoo><template id="other"/></odoo>',
    )


def test_unresolved_template_in_impact_added_file_is_non_blocking():
    service = SyntaxVerifierService()
    content = '<templates><t t-name="sample.Child" t-inherit="missing.Parent" owl="1"/></templates>'

    related_findings = service._verify_qweb_template(
        "/tmp/related.xml",
        content,
        {"/tmp/related.xml": content},
        is_explicitly_written=False,
    )
    explicit_findings = service._verify_qweb_template(
        "/tmp/written.xml",
        content,
        {"/tmp/written.xml": content},
        is_explicitly_written=True,
    )

    related_missing = [f for f in related_findings if f.check == "qweb_template_source_exists"]
    explicit_missing = [f for f in explicit_findings if f.check == "qweb_template_source_exists"]
    assert related_missing and related_missing[0].severity == VerifySeverity.WARNING
    assert explicit_missing and explicit_missing[0].severity == VerifySeverity.CRITICAL
