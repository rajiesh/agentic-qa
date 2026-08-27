import pytest
from pathlib import Path

from agentic_qa.config import QAConfig, RepoTarget
from agentic_qa.core.repo_ingestor import RepoIngestor, _repo_name


def test_repo_name_extraction():
    assert _repo_name("https://github.com/org/myapp.git") == "myapp"
    assert _repo_name("https://github.com/org/myapp") == "myapp"
    assert _repo_name("https://github.com/org/myapp/") == "myapp"


@pytest.mark.asyncio
async def test_local_path_passthrough(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    config = QAConfig()
    ingestor = RepoIngestor(config)
    result = await ingestor.prepare(RepoTarget(url=str(tmp_path)))
    assert result == tmp_path


@pytest.mark.asyncio
async def test_nonexistent_local_path_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    config = QAConfig()
    ingestor = RepoIngestor(config)
    fake_local = str(tmp_path / "nonexistent")
    # Non-local paths are treated as git URLs and will fail to clone
    with pytest.raises(Exception):
        await ingestor.prepare(RepoTarget(url=fake_local))
