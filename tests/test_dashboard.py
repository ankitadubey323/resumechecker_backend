import io

import pytest
from docx import Document
from PIL import Image

from tests.test_auth import signup

RESUME_TEXT = (
    "Jane Doe - Software Engineer. Skills: Python, FastAPI, React, PostgreSQL. "
    "Built a resume customizer web app used by 500 users."
)

DETAILS = {
    "experience_level": "experienced",
    "headline": "Backend Developer",
    "years_experience": 3,
    "skills": ["Python", "FastAPI", "python", "SQL"],
    "experiences": [
        {"company": "Acme", "role": "Developer", "start": "2022-01", "end": None, "description": "Built APIs"}
    ],
    "projects": [{"title": "Resume app", "description": "", "tech": ["Next.js"], "link": ""}],
    "education": [],
}

JD = "We are hiring a backend engineer with Python and FastAPI experience. " * 3


@pytest.fixture
def auth_client(client):
    assert signup(client).status_code == 201  # signup logs the user in
    return client


def make_pdf(text: str) -> bytes:
    """Smallest valid one-page PDF containing `text`."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n%s\nendobj\n" % (i, obj))
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref))
    return out.getvalue()


def make_docx(text: str) -> bytes:
    doc = Document()
    doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def make_png(size=(800, 600)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", size, (200, 50, 50, 128)).save(buf, format="PNG")
    return buf.getvalue()


def test_dashboard_requires_login(client):
    assert client.get("/api/dashboard").status_code == 401
    assert client.post("/api/resume", files={"file": ("r.pdf", b"%PDF-", "application/pdf")}).status_code == 401


def test_new_user_dashboard_is_empty_and_locked(auth_client):
    d = auth_client.get("/api/dashboard").json()
    assert d["user"]["email"] == "jane.doe@example.com"
    assert d["details"] is None and d["resume"] is None and d["photo_version"] is None
    assert d["completion"]["percent"] == 0
    assert d["ready_for_job_description"] is False


def test_jd_blocked_until_profile_or_resume(auth_client):
    r = auth_client.post("/api/job-descriptions", json={"text": JD})
    assert r.status_code == 409


@pytest.mark.parametrize("name,maker", [("cv.pdf", make_pdf), ("cv.docx", make_docx)])
def test_resume_upload_unlocks_jd(auth_client, name, maker):
    r = auth_client.post("/api/resume", files={"file": (name, maker(RESUME_TEXT), "application/octet-stream")})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["resume"]["filename"] == name
    assert "FastAPI" in d["resume"]["preview"]
    assert d["ready_for_job_description"] is True

    r = auth_client.post("/api/job-descriptions", json={"text": JD})
    assert r.status_code == 201
    assert r.json()["latest_job_description"]["length"] == len(JD.strip())


@pytest.mark.parametrize(
    "name,data,status",
    [
        ("cv.pdf", b"", 422),
        ("cv.txt", b"plain text resume " * 10, 415),
        ("cv.pdf", b"PK\x03\x04 not really a pdf", 415),  # extension doesn't match the bytes
        ("cv.docx", b"PK\x03\x04garbage", 422),
        ("cv.pdf", b"%PDF-1.4 garbage", 422),
        ("cv.pdf", b"%PDF-" + b"0" * (5 * 1024 * 1024), 413),
    ],
)
def test_resume_upload_rejects_bad_files(auth_client, name, data, status):
    r = auth_client.post("/api/resume", files={"file": (name, data, "application/pdf")})
    assert r.status_code == status, r.text
    assert isinstance(r.json()["detail"], str)


def test_resume_without_text_rejected(auth_client):
    r = auth_client.post("/api/resume", files={"file": ("cv.pdf", make_pdf(""), "application/pdf")})
    assert r.status_code == 422
    assert "scanned" in r.json()["detail"]


def test_save_details_dedupes_skills_and_unlocks_jd(auth_client):
    r = auth_client.put("/api/profile/details", json=DETAILS)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["details"]["skills"] == ["Python", "FastAPI", "SQL"]
    assert d["details"]["projects"][0]["link"] is None
    assert d["ready_for_job_description"] is True


def test_fresher_years_forced_to_zero(auth_client):
    body = {
        **DETAILS,
        "experience_level": "fresher",
        "years_experience": 5,
        "experiences": [],
        "education": [{"degree": "B.Tech", "institution": "IIT", "year": 2024}],
    }
    r = auth_client.put("/api/profile/details", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["details"]["years_experience"] == 0


@pytest.mark.parametrize(
    "patch,field",
    [
        ({"experience_level": "senior"}, "experience_level"),
        ({"headline": "x"}, "headline"),
        ({"years_experience": 0}, "years_experience"),
        ({"skills": ["Python", "python"]}, "skills"),
        ({"experiences": []}, "experiences"),
        ({"projects": []}, "projects"),
        ({"projects": [{"title": "", "tech": []}]}, "projects.0.title"),
        ({"projects": [{"title": "X", "link": "javascript:alert(1)"}]}, "projects.0.link"),
        ({"experiences": [{**DETAILS["experiences"][0], "start": "2023-13"}]}, "experiences.0.start"),
        ({"experiences": [{**DETAILS["experiences"][0], "start": "2099-01"}]}, "experiences.0.start"),
        ({"experiences": [{**DETAILS["experiences"][0], "end": "2021-01"}]}, "experiences.0"),
        ({"experience_level": "fresher", "education": []}, "education"),
    ],
)
def test_details_validation(auth_client, patch, field):
    r = auth_client.put("/api/profile/details", json={**DETAILS, **patch})
    assert r.status_code == 422
    assert field in r.json()["errors"], r.json()


def test_jd_validation(auth_client):
    auth_client.put("/api/profile/details", json=DETAILS)
    assert auth_client.post("/api/job-descriptions", json={"text": "   "}).status_code == 422
    r = auth_client.post("/api/job-descriptions", json={"text": "too short"})
    assert r.status_code == 422 and "text" in r.json()["errors"]
    assert auth_client.post("/api/job-descriptions", json={"text": "x" * 20_001}).status_code == 422


def test_photo_upload_is_reencoded(auth_client):
    r = auth_client.post("/api/profile/photo", files={"file": ("me.png", make_png(), "image/png")})
    assert r.status_code == 200, r.text
    assert r.json()["photo_version"] is not None

    photo = auth_client.get("/api/profile/photo")
    assert photo.headers["content-type"] == "image/webp"
    img = Image.open(io.BytesIO(photo.content))
    assert img.format == "WEBP" and img.size == (400, 400)

    r = auth_client.delete("/api/profile/photo")
    assert r.json()["photo_version"] is None
    assert auth_client.get("/api/profile/photo").status_code == 404


@pytest.mark.parametrize(
    "data,status",
    [(b"<svg onload=alert(1)></svg>", 415), (b"GIF89a" + b"0" * 100, 415), (b"0" * (2 * 1024 * 1024 + 1), 413)],
)
def test_photo_rejects_bad_files(auth_client, data, status):
    r = auth_client.post("/api/profile/photo", files={"file": ("x.png", data, "image/png")})
    assert r.status_code == status


def test_completion_reaches_100(auth_client):
    auth_client.put("/api/profile/details", json=DETAILS)
    auth_client.post("/api/profile/photo", files={"file": ("me.png", make_png(), "image/png")})
    r = auth_client.post("/api/resume", files={"file": ("cv.docx", make_docx(RESUME_TEXT), "x")})
    assert r.json()["completion"] == {"percent": 100, "missing": []}


def test_datetimes_are_sent_as_utc(auth_client):
    r = auth_client.post("/api/resume", files={"file": ("cv.docx", make_docx(RESUME_TEXT), "x")})
    uploaded_at = r.json()["resume"]["uploaded_at"]
    assert uploaded_at.endswith("Z") or uploaded_at.endswith("+00:00"), uploaded_at
