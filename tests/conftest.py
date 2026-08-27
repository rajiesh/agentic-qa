import pytest


@pytest.fixture
def sample_repo_path(tmp_path):
    """Create a minimal fake Python repo for testing."""
    src = tmp_path / "src"
    src.mkdir()

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\n[tool.pytest.ini_options]\n'
    )
    (src / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n\n@app.get('/health')\ndef health():\n    return {'status': 'ok'}\n"
    )
    (src / "models.py").write_text(
        "from pydantic import BaseModel\n\nclass User(BaseModel):\n    id: int\n    name: str\n"
    )
    return tmp_path
