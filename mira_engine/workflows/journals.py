"""Curated, versioned journal writing profiles; never load a user-supplied prompt path."""

from pathlib import Path

PROFILE_VERSION = "2026-10-12.3"
SOURCE_CHECK_DATE = "2026-10-12"
_ROOT = Path(__file__).resolve().parents[1] / "templates" / "journals"

# Hard requirements live in the corresponding prompt, separately from MIRA's
# editorial recommendations. Sources are publisher/society pages, not aggregators.
_JOURNALS = (
    ("radiology", "Radiology", "Clinical radiology / 临床影像", "Original Research",
     "https://pubs.rsna.org/journal/radiology", "https://pubs.rsna.org/page/radiology/author-instructions", "verified"),
    ("radiology_ai", "Radiology: Artificial Intelligence", "Clinical imaging AI / 临床影像 AI", "Original Research",
     "https://pubs.rsna.org/journal/ai", "https://pubs.rsna.org/page/ai/author-instructions", "verified"),
    ("mrm", "Magnetic Resonance in Medicine (MRM)", "MR methodology / 磁共振方法学", "Research Article",
     "https://onlinelibrary.wiley.com/journal/15222594", "https://onlinelibrary.wiley.com/page/journal/15222594/homepage/author-guidelines", "verified"),
    ("jmri", "Journal of Magnetic Resonance Imaging (JMRI)", "Clinical MRI / 临床磁共振", "Research Article",
     "https://onlinelibrary.wiley.com/journal/15222586", "https://onlinelibrary.wiley.com/page/journal/15222586/homepage/forauthors.html", "verified"),
    ("media", "Medical Image Analysis (MedIA)", "Image analysis methodology / 医学图像分析方法", "Research Article",
     "https://www.sciencedirect.com/journal/medical-image-analysis", "https://www.sciencedirect.com/journal/medical-image-analysis/publish/guide-for-authors", "verified"),
    ("ieee_tmi", "IEEE Transactions on Medical Imaging (TMI)", "Imaging engineering / 医学成像工程", "Regular Paper (initial submission)",
     "https://ieeetmi.org/", "https://ieeetmi.org/authors-instructions/", "verified"),
    ("european_radiology", "European Radiology", "Clinical translation / 临床转化", "Original Article",
     "https://journals.myesr.org/eur-radiol/", "https://journals.myesr.org/eur-radiol/for-authors/article-types/", "verified"),
)


def journal_catalog() -> list[dict[str, str]]:
    return [dict(zip(
        ("id", "name", "focus", "article_type", "homepage", "guidelines_url", "verification_status"), row,
        strict=True,
    ), profile_version=PROFILE_VERSION, checked_at=SOURCE_CHECK_DATE) for row in _JOURNALS]


def normalize_journal_selection(module_type: str, data: dict) -> dict:
    """Validate persisted selection at creation, draft edits and executor entry."""
    result = dict(data)
    journal_id = result.get("target_journal")
    if journal_id is None:
        if "journal_profile_version" in result:
            raise ValueError("journal_profile_version requires target_journal")
        return result
    if module_type != "delivery" or result.get("delivery_format") != "paper_article":
        raise ValueError("target_journal is only supported for paper_article delivery")
    if not isinstance(journal_id, str) or journal_id not in {item[0] for item in _JOURNALS}:
        raise ValueError("unknown target_journal; choose a journal from the catalog")
    if result.get("journal_profile_version", PROFILE_VERSION) != PROFILE_VERSION:
        raise ValueError("journal profile version changed; reload the catalog and confirm the journal")
    result["journal_profile_version"] = PROFILE_VERSION
    return result


def journal_instructions(data: dict) -> str:
    selection = normalize_journal_selection("delivery", data)
    journal_id = selection.get("target_journal")
    if not journal_id:
        return ""
    journal = next(item for item in journal_catalog() if item["id"] == journal_id)
    common = (_ROOT / "common.md").read_text(encoding="utf-8")
    specific = (_ROOT / f"{journal_id}.md").read_text(encoding="utf-8")
    return (
        f"\n\nTarget journal: {journal['name']}\nArticle type: {journal['article_type']}\n"
        f"Profile version: {PROFILE_VERSION}\nSource check: {SOURCE_CHECK_DATE}\n"
        f"Verification status: {journal['verification_status']}\n"
        f"Official homepage: {journal['homepage']}\nAuthor instructions: {journal['guidelines_url']}\n\n"
        f"{common}\n\n{specific}"
    )
