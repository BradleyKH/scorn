import pytest

from scorn.reconcile import pdf_score


def test_scan_is_not_applicable(scores_dir):
    pdf = scores_dir / "duke street.pdf"
    if not pdf.exists():
        pytest.skip(f"missing corpus scan: {pdf}")
    result = pdf_score.read_pdf(pdf)
    assert isinstance(result, pdf_score.NotApplicable)
