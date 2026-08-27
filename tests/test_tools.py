import pytest

from agentic_qa.tools.repo_tools import async_list_directory, async_read_file, async_search_code


@pytest.mark.asyncio
async def test_read_file(sample_repo_path):
    content = await async_read_file("src/main.py", repo_root=str(sample_repo_path))
    assert "FastAPI" in content
    assert "health" in content


@pytest.mark.asyncio
async def test_read_file_missing(sample_repo_path):
    result = await async_read_file("nonexistent.py", repo_root=str(sample_repo_path))
    assert "[error]" in result


@pytest.mark.asyncio
async def test_list_directory(sample_repo_path):
    tree = await async_list_directory(".", repo_root=str(sample_repo_path), depth=2)
    assert "src" in tree
    assert "pyproject.toml" in tree


@pytest.mark.asyncio
async def test_search_code(sample_repo_path):
    result = await async_search_code("FastAPI", repo_root=str(sample_repo_path))
    assert "main.py" in result
    assert "FastAPI" in result


@pytest.mark.asyncio
async def test_search_code_no_results(sample_repo_path):
    result = await async_search_code(
        "XYZNOTFOUND123", repo_root=str(sample_repo_path)
    )
    assert "no results" in result.lower() or result == ""
