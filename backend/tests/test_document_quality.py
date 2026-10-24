from io import BytesIO

from PIL import Image
from pypdf import PdfWriter

from app.intake import _analyse_document, _stage_role


def test_small_flat_image_is_flagged_for_quality_issues():
    image = Image.new("RGB", (240, 160), color="white")
    output = BytesIO()
    image.save(output, format="PNG")

    findings = _analyse_document(
        "Institution Registration / Approval Proof",
        "registration.png",
        output.getvalue(),
        "Example Institute",
    )

    assert any("resolution" in finding.lower() for finding in findings)
    assert any("blurred" in finding.lower() for finding in findings)


def test_pdf_without_extractable_text_is_flagged():
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    output = BytesIO()
    writer.write(output)

    findings = _analyse_document(
        "Land / Building Evidence",
        "land.pdf",
        output.getvalue(),
        "Example Institute",
    )

    assert any("extractable text" in finding.lower() for finding in findings)


def test_stage_role_mapping_is_explicit():
    assert _stage_role("Scrutiny") == "scrutiny_officer"
    assert _stage_role("EVC Visit") == "evc_officer"
    assert _stage_role("SAC/SHC Review") == "sac_shc_officer"
    assert _stage_role("Compliance") == "compliance_officer"
    assert _stage_role("Unknown stage") is None
