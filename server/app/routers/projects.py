from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..deps import get_session
from ..models import Project, ResultSet
from ..schemas import ProjectCreate, ProjectOut, ProjectUpdate
from ..timeutil import as_utc

router = APIRouter(prefix="/api/projects", tags=["results"])


def _count(session: Session, project_id: int) -> int:
    return session.scalar(select(func.count()).select_from(ResultSet).where(ResultSet.project_id == project_id)) or 0


def project_out(session: Session, project: Project) -> ProjectOut:
    return ProjectOut(
        id=project.id,
        name=project.name,
        description=project.description,
        result_count=_count(session, project.id),
        created_at=as_utc(project.created_at),
    )


def _load(session: Session, project_id: int) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    return project


def _check_name(session: Session, name: str, project_id: int | None = None) -> None:
    existing = session.scalar(select(Project).where(Project.name == name))
    if existing is not None and existing.id != project_id:
        raise HTTPException(409, f"项目 {name} 已存在")


@router.get("", response_model=list[ProjectOut], summary="项目列表")
def list_projects(session: Session = Depends(get_session)):
    return [project_out(session, p) for p in session.scalars(select(Project).order_by(Project.name))]


@router.post("", response_model=ProjectOut, status_code=201, summary="创建项目")
def create_project(body: ProjectCreate, session: Session = Depends(get_session)):
    name = body.name.strip()
    _check_name(session, name)
    project = Project(name=name, description=body.description)
    session.add(project)
    session.commit()
    return project_out(session, project)


@router.patch("/{project_id}", response_model=ProjectOut, summary="修改项目")
def update_project(project_id: int, body: ProjectUpdate, session: Session = Depends(get_session)):
    project = _load(session, project_id)
    fields = body.model_dump(exclude_unset=True)
    if "name" in fields:
        if fields["name"] is None:
            raise HTTPException(422, "name 不能为空")
        fields["name"] = fields["name"].strip()
        _check_name(session, fields["name"], project_id)
    for field, value in fields.items():
        setattr(project, field, value)
    session.commit()
    return project_out(session, project)


@router.delete("/{project_id}", status_code=204, summary="删除项目（其中的结果集改为未归档，不会删除）")
def delete_project(project_id: int, session: Session = Depends(get_session)):
    project = _load(session, project_id)
    for result in session.scalars(select(ResultSet).where(ResultSet.project_id == project_id)):
        result.project_id = None
    session.delete(project)
    session.commit()
    return Response(status_code=204)
