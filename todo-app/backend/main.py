from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db, create_tables
from models import Todo
from schemas import TodoCreate, TodoUpdate, TodoResponse


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_tables()
    yield


app = FastAPI(title="Todo API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/todos", response_model=list[TodoResponse])
async def list_todos(db: AsyncSession = Depends(get_db)):
    """Return all todo items ordered by creation date descending."""
    result = await db.execute(select(Todo).order_by(Todo.created_at.desc()))
    return result.scalars().all()


@app.post("/todos", response_model=TodoResponse, status_code=201)
async def create_todo(payload: TodoCreate, db: AsyncSession = Depends(get_db)):
    """Save a new todo item."""
    todo = Todo(title=payload.title, description=payload.description)
    db.add(todo)
    await db.commit()
    await db.refresh(todo)
    return todo


@app.put("/todos/{todo_id}", response_model=TodoResponse)
async def update_todo(
    todo_id: int, payload: TodoUpdate, db: AsyncSession = Depends(get_db)
):
    """Update an existing todo item's title, description, or completed status."""
    result = await db.execute(select(Todo).where(Todo.id == todo_id))
    todo = result.scalar_one_or_none()
    if todo is None:
        raise HTTPException(status_code=404, detail=f"Todo {todo_id} not found")

    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(todo, field, value)

    await db.commit()
    await db.refresh(todo)
    return todo


@app.delete("/todos/{todo_id}", status_code=204)
async def delete_todo(todo_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a todo item permanently."""
    result = await db.execute(select(Todo).where(Todo.id == todo_id))
    todo = result.scalar_one_or_none()
    if todo is None:
        raise HTTPException(status_code=404, detail=f"Todo {todo_id} not found")

    await db.delete(todo)
    await db.commit()


@app.get("/health")
async def health():
    return {"status": "ok"}
