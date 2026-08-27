const API_BASE = 'http://localhost:8000';

let todos = [];
let currentFilter = 'all';

// ── API helpers ──────────────────────────────────────────────────────────────

async function apiFetch(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || 'Request failed');
  }
  if (res.status === 204) return null;
  return res.json();
}

const api = {
  list:   ()           => apiFetch('/todos'),
  create: (data)       => apiFetch('/todos', { method: 'POST', body: JSON.stringify(data) }),
  update: (id, data)   => apiFetch(`/todos/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  delete: (id)         => apiFetch(`/todos/${id}`, { method: 'DELETE' }),
};

// ── State helpers ─────────────────────────────────────────────────────────────

function visibleTodos() {
  if (currentFilter === 'active')    return todos.filter(t => !t.completed);
  if (currentFilter === 'completed') return todos.filter(t => t.completed);
  return todos;
}

// ── Render ───────────────────────────────────────────────────────────────────

function render() {
  const list = document.getElementById('todo-list');
  const visible = visibleTodos();

  if (visible.length === 0) {
    list.innerHTML = `<div class="loading">${
      currentFilter === 'all' ? 'No todos yet — add one above!' :
      currentFilter === 'active' ? 'Nothing active. ✓' : 'Nothing completed yet.'
    }</div>`;
  } else {
    list.innerHTML = visible.map(todoHTML).join('');
    attachItemListeners();
  }

  updateFooter();
}

function todoHTML(todo) {
  return `
    <div class="todo-item ${todo.completed ? 'completed' : ''}" data-id="${todo.id}">
      <input type="checkbox" class="toggle" ${todo.completed ? 'checked' : ''} />
      <div class="todo-body">
        <div class="todo-title view-mode">${escHtml(todo.title)}</div>
        ${todo.description ? `<div class="todo-desc view-mode">${escHtml(todo.description)}</div>` : ''}
      </div>
      <div class="todo-actions">
        <button class="btn-edit">Edit</button>
        <button class="btn-delete">Delete</button>
      </div>
    </div>`;
}

function attachItemListeners() {
  document.querySelectorAll('.todo-item').forEach(item => {
    const id = Number(item.dataset.id);

    item.querySelector('.toggle').addEventListener('change', e => {
      toggleComplete(id, e.target.checked);
    });

    item.querySelector('.btn-edit').addEventListener('click', () => {
      enterEditMode(item, id);
    });

    item.querySelector('.btn-delete').addEventListener('click', () => {
      removeTodo(id);
    });
  });
}

function enterEditMode(item, id) {
  const todo = todos.find(t => t.id === id);
  const body = item.querySelector('.todo-body');

  body.innerHTML = `
    <input class="edit-input" id="edit-title-${id}" type="text"
           value="${escAttr(todo.title)}" maxlength="255" />
    <input class="edit-input" id="edit-desc-${id}" type="text"
           value="${escAttr(todo.description || '')}" maxlength="500"
           placeholder="Description (optional)" />`;

  const actions = item.querySelector('.todo-actions');
  actions.innerHTML = `
    <button class="btn-save">Save</button>
    <button class="btn-cancel">Cancel</button>`;

  document.getElementById(`edit-title-${id}`).focus();

  actions.querySelector('.btn-save').addEventListener('click', () => {
    saveEdit(id);
  });
  actions.querySelector('.btn-cancel').addEventListener('click', render);

  // Save on Enter key in either input
  body.querySelectorAll('.edit-input').forEach(input => {
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter') saveEdit(id);
      if (e.key === 'Escape') render();
    });
  });
}

function updateFooter() {
  const footer = document.getElementById('footer');
  const count = document.getElementById('count');
  const clearBtn = document.getElementById('clear-completed');
  const active = todos.filter(t => !t.completed).length;
  const completed = todos.filter(t => t.completed).length;

  if (todos.length === 0) {
    footer.classList.add('hidden');
    return;
  }
  footer.classList.remove('hidden');
  count.textContent = `${active} item${active !== 1 ? 's' : ''} left`;
  clearBtn.style.visibility = completed > 0 ? 'visible' : 'hidden';
}

// ── Actions ───────────────────────────────────────────────────────────────────

async function loadTodos() {
  try {
    todos = await api.list();
    render();
  } catch (err) {
    showToast('Failed to load todos: ' + err.message, true);
    document.getElementById('todo-list').innerHTML =
      '<div class="loading">Could not connect to API. Is the server running?</div>';
  }
}

async function addTodo(title, description) {
  try {
    const todo = await api.create({ title, description: description || null });
    todos.unshift(todo);
    render();
    showToast('Todo added');
  } catch (err) {
    showToast('Error: ' + err.message, true);
  }
}

async function toggleComplete(id, completed) {
  try {
    const updated = await api.update(id, { completed });
    todos = todos.map(t => t.id === id ? updated : t);
    render();
  } catch (err) {
    showToast('Error: ' + err.message, true);
    render(); // revert checkbox
  }
}

async function saveEdit(id) {
  const title = document.getElementById(`edit-title-${id}`)?.value.trim();
  const description = document.getElementById(`edit-desc-${id}`)?.value.trim() || null;

  if (!title) { showToast('Title cannot be empty', true); return; }

  try {
    const updated = await api.update(id, { title, description });
    todos = todos.map(t => t.id === id ? updated : t);
    render();
    showToast('Todo updated');
  } catch (err) {
    showToast('Error: ' + err.message, true);
  }
}

async function removeTodo(id) {
  try {
    await api.delete(id);
    todos = todos.filter(t => t.id !== id);
    render();
    showToast('Todo deleted');
  } catch (err) {
    showToast('Error: ' + err.message, true);
  }
}

async function clearCompleted() {
  const completed = todos.filter(t => t.completed);
  try {
    await Promise.all(completed.map(t => api.delete(t.id)));
    todos = todos.filter(t => !t.completed);
    render();
    showToast(`Cleared ${completed.length} completed item${completed.length !== 1 ? 's' : ''}`);
  } catch (err) {
    showToast('Error: ' + err.message, true);
    await loadTodos();
  }
}

// ── Utils ─────────────────────────────────────────────────────────────────────

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function escAttr(str) {
  return String(str).replace(/"/g, '&quot;');
}

let toastTimer;
function showToast(msg, isError = false) {
  const toast = document.getElementById('toast');
  toast.textContent = msg;
  toast.classList.toggle('error', isError);
  toast.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.add('hidden'), 3000);
}

// ── Bootstrap ─────────────────────────────────────────────────────────────────

document.getElementById('add-form').addEventListener('submit', e => {
  e.preventDefault();
  const title = document.getElementById('new-title').value.trim();
  const description = document.getElementById('new-description').value.trim();
  if (!title) return;
  addTodo(title, description);
  e.target.reset();
});

document.querySelectorAll('.filter-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentFilter = btn.dataset.filter;
    render();
  });
});

document.getElementById('clear-completed').addEventListener('click', clearCompleted);

loadTodos();
