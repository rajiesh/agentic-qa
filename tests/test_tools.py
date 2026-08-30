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


# ── async_read_file offset / pagination (Phase 3) ─────────────────────────────

@pytest.mark.asyncio
async def test_read_file_offset_zero_default_unchanged(sample_repo_path):
    """offset=0 (default) must behave identically to before — no regression."""
    result = await async_read_file("src/main.py", repo_root=str(sample_repo_path))
    assert "FastAPI" in result


@pytest.mark.asyncio
async def test_read_file_offset_skips_earlier_lines(tmp_path):
    """Lines before the offset must not appear in the output."""
    f = tmp_path / "big.py"
    f.write_text("\n".join(f"line_{i}" for i in range(20)))

    # Read the first 5 lines
    first = await async_read_file("big.py", repo_root=str(tmp_path), max_lines=5, offset=0)
    assert "line_0" in first
    assert "line_5" not in first

    # Read the next 5 lines
    second = await async_read_file("big.py", repo_root=str(tmp_path), max_lines=5, offset=5)
    assert "line_5" in second
    assert "line_0" not in second
    assert "line_10" not in second


@pytest.mark.asyncio
async def test_read_file_offset_continuation_hint(tmp_path):
    """When lines remain beyond the window, a '… [N more lines not shown]' footer appears."""
    f = tmp_path / "long.py"
    f.write_text("\n".join(f"line_{i}" for i in range(10)))

    result = await async_read_file("long.py", repo_root=str(tmp_path), max_lines=3, offset=0)
    assert "more lines not shown" in result
    assert "offset=3" in result  # next-offset hint for the agent


@pytest.mark.asyncio
async def test_read_file_offset_no_hint_when_fully_read(tmp_path):
    """When all lines fit in the window, no pagination footer is added."""
    f = tmp_path / "short.py"
    f.write_text("a\nb\nc")

    result = await async_read_file("short.py", repo_root=str(tmp_path), max_lines=100, offset=0)
    assert "more lines not shown" not in result
    assert "end of file" not in result  # offset==0, no footer at all


@pytest.mark.asyncio
async def test_read_file_offset_at_end_shows_eof_marker(tmp_path):
    """When offset reads the final page, an end-of-file marker is appended."""
    f = tmp_path / "medium.py"
    f.write_text("\n".join(f"line_{i}" for i in range(10)))

    result = await async_read_file("medium.py", repo_root=str(tmp_path), max_lines=5, offset=5)
    assert "end of file" in result
    assert "line_5" in result
    assert "line_9" in result


@pytest.mark.asyncio
async def test_read_file_offset_out_of_bounds_returns_empty_gracefully(tmp_path):
    """An offset beyond the file end should return an eof marker, not an error."""
    f = tmp_path / "tiny.py"
    f.write_text("only one line")

    result = await async_read_file("tiny.py", repo_root=str(tmp_path), max_lines=5, offset=100)
    # No crash; either empty content or an eof marker
    assert "[error]" not in result


@pytest.mark.asyncio
async def test_read_file_pages_cover_whole_file(tmp_path):
    """Reading a file page-by-page should reconstruct every line."""
    total_lines = 25
    f = tmp_path / "pageable.py"
    f.write_text("\n".join(f"content_{i}" for i in range(total_lines)))

    page_size = 10
    all_content = ""
    offset = 0
    while True:
        chunk = await async_read_file(
            "pageable.py", repo_root=str(tmp_path), max_lines=page_size, offset=offset
        )
        all_content += chunk
        if "more lines not shown" not in chunk:
            break
        offset += page_size

    for i in range(total_lines):
        assert f"content_{i}" in all_content
