from pathlib import Path


SUPERVISORD_CONF = Path(__file__).resolve().parents[2] / "docker" / "backend" / "supervisord.conf"


def test_supervisord_app_programs_set_home_for_non_root_user():
    content = SUPERVISORD_CONF.read_text(encoding="utf-8")
    for program in ("api", "aps"):
        section_start = content.index(f"[program:{program}]")
        next_section = content.find("\n[", section_start + 1)
        section = content[section_start:] if next_section == -1 else content[section_start:next_section]
        assert "user=app" in section
        assert 'HOME="/home/app"' in section
