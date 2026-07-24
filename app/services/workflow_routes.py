"""Явная карта допустимых переходов производственного процесса."""

TASK_ROUTES = {
    "procurement_purchase": {"accounting_payment"},
    "accounting_payment": {"warehouse_receive_components"},
    "warehouse_receive_components": {
        "warehouse_issue_materials", "repair_issue_materials", "procurement_purchase",
    },
    "warehouse_issue_materials": {"assembler_receive_materials"},
    "assembler_receive_materials": {"assembler_build", "warehouse_issue_materials", "procurement_purchase"},
    "assembler_build": {"tester_check", "repair_issue_materials", "procurement_purchase"},
    "tester_check": {"packer_pack", "repair_defects"},
    "repair_defects": {"tester_check", "repair_issue_materials", "procurement_purchase"},
    "repair_issue_materials": {"assembler_receive_materials", "repair_receive_materials"},
    "repair_receive_materials": set(),
    "packer_pack": {"warehouse_finished_goods"},
    "warehouse_finished_goods": set(),
}

TASK_ROLES = {
    "procurement_purchase": "procurement",
    "accounting_payment": "accounting",
    "warehouse_receive_components": "warehouse",
    "warehouse_issue_materials": "warehouse",
    "assembler_receive_materials": "assembler",
    "assembler_build": "assembler",
    "tester_check": "tester",
    "repair_defects": "repair_engineer",
    "repair_issue_materials": "warehouse",
    "repair_receive_materials": "repair_engineer",
    "packer_pack": "packer",
    "warehouse_finished_goods": "warehouse",
}


def validate_task_role(task_type: str, role: str):
    expected = TASK_ROLES.get(task_type)
    if expected and role != expected:
        raise ValueError(f"Задача {task_type} должна принадлежать роли {expected}, получена {role}")


def validate_transition(source_type: str, target_type: str):
    allowed = TASK_ROUTES.get(source_type)
    if allowed is not None and target_type not in allowed:
        raise ValueError(f"Недопустимый маршрут {source_type} -> {target_type}")
