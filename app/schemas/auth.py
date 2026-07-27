from typing import List, Optional
from pydantic import BaseModel, Field


VALID_ROLES = [
    "admin",
    "warehouse",
    "manager",
    "engineer",
    "procurement",
    "accounting",
    "assembler",
    "tester",
    "repair_engineer",
    "packer",
    "production",
    "production_manager",
]


class LoginRequest(BaseModel):
    phone: Optional[str] = None
    username: Optional[str] = None
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserCreate(BaseModel):
    username: Optional[str] = Field(default=None, min_length=3)
    password: str = Field(min_length=6)
    full_name: Optional[str] = None
    last_name: Optional[str] = None
    first_name: Optional[str] = None
    middle_name: Optional[str] = None
    phone: Optional[str] = None
    role: str = Field(default="manager")
    roles: Optional[List[str]] = None
    task_roles: Optional[List[str]] = None
    auto_tasks_enabled: bool = True
    manual_assignment_enabled: bool = True


class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    last_name: Optional[str] = None
    first_name: Optional[str] = None
    middle_name: Optional[str] = None
    phone: Optional[str] = None
    role: Optional[str] = None
    roles: Optional[List[str]] = None
    task_roles: Optional[List[str]] = None
    auto_tasks_enabled: Optional[bool] = None
    manual_assignment_enabled: Optional[bool] = None
    is_active: Optional[bool] = None
    password: Optional[str] = Field(default=None, min_length=6)


class UserOut(BaseModel):
    id: int
    username: str
    full_name: Optional[str] = None
    last_name: Optional[str] = None
    first_name: Optional[str] = None
    middle_name: Optional[str] = None
    phone: Optional[str] = None
    role: str
    roles: List[str] = Field(default_factory=list)
    task_roles: List[str] = Field(default_factory=list)
    auto_tasks_enabled: bool = True
    manual_assignment_enabled: bool = True
    is_active: bool

    class Config:
        from_attributes = True


class MeResponse(UserOut):
    permissions: List[str]
