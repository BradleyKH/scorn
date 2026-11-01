import pytest

from scorn.reconcile import reconcile


def test_not_applicable_passes_input_through(scores_dir, tmp_path):
    pdf = scores_dir / "duke street.pdf"
    if not pdf.exists():
        pytest.skip(f"missing image-only scan: {pdf}")
    src = tmp_path / "in.musicxml"
    src.write_bytes(b'<?xml version="1.0" encoding="UTF-8"?>\n<score-partwise version="4.0"><part-list/></score-partwise>\n')
    out = tmp_path / "out.musicxml"
    report = reconcile(pdf, src, out)
    assert not report.applicable and report.reason
    assert out.read_bytes() == src.read_bytes()
    assert "left exactly as it was" in report.summary()
    assert report.reason not in report.summary() and report.to_dict()["reason"] == report.reason
