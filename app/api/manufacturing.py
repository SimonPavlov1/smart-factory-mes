from datetime import date, datetime
from io import BytesIO
from urllib.parse import quote
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from typing import List

from app.database import get_db
from app.models.auth import User
from app.models.production import Item, Order, OrderItem, ProductBOM, ProductType, Reservation, WorkflowTask
from app.models.inventory import Stock, Component  # Component используется для вытягивания наименований деталей
from app.services.reservation_service import reserve_components
from app.services.production_planning import get_bom_requirements
from app.services.auth_service import require_roles, user_roles
from app.services.workflow_service import complete_task, create_initial_order_tasks, create_procurement_task_for_order, find_order_shortages, find_shortages, plan_material_availability, reconcile_stock_reservations
from app.services.xlsx_service import build_table_xlsx
from app.services.order_progress_service import aggregate_order_progress

# ИМПОРТ СХЕМ: Подтягиваем переписанные схемы из файла
from app.schemas.order import OrderCreate, OrderOut

router = APIRouter(prefix="/manufacturing", tags=["Производство (Заказы)"])

ORDER_STAGES = [
    {
        "key": "procurement",
        "title": "Закупка",
        "description": "Оформление недостающих комплектующих",
        "task_types": ["procurement_purchase"],
    },
    {
        "key": "accounting",
        "title": "Оплата",
        "description": "Оплата счетов по закупке",
        "task_types": ["accounting_payment"],
    },
    {
        "key": "warehouse_receive",
        "title": "Приемка на склад",
        "description": "Приход комплектующих от поставщика",
        "task_types": ["warehouse_receive_components"],
    },
    {
        "key": "warehouse_issue",
        "title": "Выдача комплектующих",
        "description": "Передача комплекта сборщику",
        "task_types": ["warehouse_issue_materials", "repair_issue_materials"],
    },
    {
        "key": "assembler_receive",
        "title": "Получение сборщиком",
        "description": "Подтверждение получения комплекта",
        "task_types": ["assembler_receive_materials"],
    },
    {
        "key": "repair_receive",
        "title": "Получение отделом брака",
        "description": "Подтверждение получения дополнительных компонентов инженером по ремонту",
        "task_types": ["repair_receive_materials"],
    },
    {
        "key": "assembly",
        "title": "Сборка",
        "description": "Сборка изделий по заказу",
        "task_types": ["assembler_build"],
    },
    {
        "key": "testing",
        "title": "Тестирование",
        "description": "Проверка и фиксация брака",
        "task_types": ["tester_check"],
    },
    {
        "key": "repair",
        "title": "Ремонт брака",
        "description": "Устранение выявленных дефектов",
        "task_types": ["repair_defects"],
    },
    {
        "key": "packing",
        "title": "Упаковка",
        "description": "Передача годных изделий на упаковку",
        "task_types": ["packer_pack"],
    },
    {
        "key": "finished_goods",
        "title": "Склад готовой продукции",
        "description": "Оприходование готовых изделий",
        "task_types": ["warehouse_finished_goods"],
    },
]


def _add_material(total_needed_items: dict, component_id: int, qty: float):
    if not component_id:
        raise HTTPException(status_code=400, detail="В BOM есть непривязанная позиция без складского компонента")
    if qty <= 0:
        raise HTTPException(status_code=400, detail=f"Некорректное количество компонента ID {component_id}: {qty}")

    if component_id in total_needed_items:
        total_needed_items[component_id]["qty"] += qty
    else:
        total_needed_items[component_id] = {"component_id": component_id, "qty": qty}


def _calculate_materials_for_items(order_items, db: Session):
    total_needed_items = {}

    for item in order_items:
        if item.quantity <= 0:
            raise HTTPException(status_code=400, detail=f"Некорректное количество изделия ID {item.product_id}")

        needed = get_bom_requirements(item.product_id, item.quantity, db)
        for material in needed:
            _add_material(total_needed_items, material["component_id"], material["qty"])

    return total_needed_items


def _materials_list(total_needed_items: dict):
    return list(total_needed_items.values())


def _product_label(product: ProductType):
    if not product:
        return "Неизвестное изделие"
    return product.name if not product.sku else f"{product.name} ({product.sku})"


def _append_bom_summary_item(result_map: dict, item_data: dict):
    key = (
        item_data["device"],
        item_data["assembly"],
        item_data["category"],
        item_data["name"],
        item_data["sku"],
    )

    if key not in result_map:
        result_map[key] = item_data
        return

    result_map[key]["qty"] += item_data["qty"]
    if item_data.get("designators"):
        existing = result_map[key].get("designators")
        result_map[key]["designators"] = (
            f"{existing}, {item_data['designators']}" if existing else item_data["designators"]
        )


def _collect_structured_bom(product_id: int, multiplier: float, device: str, assembly: str, result_map: dict,
                            db: Session, visited=None):
    visited = visited or set()
    if product_id in visited:
        raise HTTPException(status_code=400, detail=f"Обнаружен циклический BOM у изделия ID {product_id}")

    current_visited = visited | {product_id}
    bom_items = db.query(ProductBOM).filter(ProductBOM.product_id == product_id).all()
    children_by_parent = {}
    for item in bom_items:
        if item.parent_id:
            children_by_parent.setdefault(item.parent_id, []).append(item)

    def visit_bom(bom, current_multiplier, current_assembly):
        item_type = bom.item_type or ("assembly" if bom.resource_type in ["product", "subassembly"] else "component")
        total_qty = bom.quantity * current_multiplier

        if item_type == "operation":
            _append_bom_summary_item(result_map, {
                "id": f"operation-{bom.id}",
                "component_id": None,
                "bom_item_id": bom.id,
                "name": bom.design_name,
                "sku": bom.operation_role or "—",
                "qty": total_qty,
                "device": device,
                "assembly": current_assembly,
                "category": "Работы и операции",
                "designators": bom.designators,
                "item_type": "operation",
            })
            return

        if item_type == "component" and bom.resource_type == "component":
            component = db.query(Component).filter(Component.id == bom.resource_id).first() if bom.resource_id else None
            _append_bom_summary_item(result_map, {
                "id": component.id if component else f"bom-{bom.id}",
                "component_id": component.id if component else None,
                "bom_item_id": bom.id,
                "name": component.name if component else bom.design_name,
                "sku": component.part_number if component else "—",
                "qty": total_qty,
                "device": device,
                "assembly": current_assembly,
                "category": component.category if component and component.category else "Покупные компоненты",
                "designators": bom.designators,
                "item_type": "purchased_component" if component else "unresolved_purchase",
            })
            return

        if item_type == "assembly":
            sub_product = db.query(ProductType).filter(ProductType.id == bom.resource_id).first() if bom.resource_id else None
            subassembly_name = _product_label(sub_product) if sub_product else bom.design_name
            assembly_path = (
                subassembly_name if current_assembly == "Основной состав" else f"{current_assembly} / {subassembly_name}"
            )

            if sub_product and db.query(ProductBOM).filter(ProductBOM.product_id == sub_product.id).first():
                _collect_structured_bom(
                    product_id=sub_product.id,
                    multiplier=total_qty,
                    device=device,
                    assembly=assembly_path,
                    result_map=result_map,
                    db=db,
                    visited=current_visited,
                )
                return

            local_children = children_by_parent.get(bom.id, [])
            if local_children:
                for child in local_children:
                    visit_bom(child, total_qty, assembly_path)
                return

            _append_bom_summary_item(result_map, {
                "id": sub_product.id if sub_product else f"bom-{bom.id}",
                "component_id": None,
                "bom_item_id": bom.id,
                "name": subassembly_name,
                "sku": sub_product.sku if sub_product and sub_product.sku else "—",
                "qty": total_qty,
                "device": device,
                "assembly": current_assembly,
                "category": "Покупные изделия и узлы",
                "designators": bom.designators,
                "item_type": "purchased_product",
            })
            return

        _append_bom_summary_item(result_map, {
            "id": f"bom-{bom.id}",
            "component_id": None,
            "bom_item_id": bom.id,
            "name": bom.design_name,
            "sku": "—",
            "qty": total_qty,
            "device": device,
            "assembly": current_assembly,
            "category": "Непривязанные позиции",
            "designators": bom.designators,
            "item_type": "unresolved_purchase",
        })

    for bom in bom_items:
        if bom.parent_id:
            continue
        visit_bom(bom, multiplier, assembly)


def _structured_bom_summary_for_order(order_items, db: Session):
    result_map = {}

    for item in order_items:
        if item.quantity <= 0:
            raise HTTPException(status_code=400, detail=f"Некорректное количество изделия ID {item.product_id}")

        product = db.query(ProductType).filter(ProductType.id == item.product_id).first()
        if not product:
            raise HTTPException(status_code=404, detail=f"Изделие ID {item.product_id} не найдено")

        device = _product_label(product)
        _collect_structured_bom(
            product_id=product.id,
            multiplier=item.quantity,
            device=device,
            assembly="Основной состав",
            result_map=result_map,
            db=db,
        )

    return sorted(
        result_map.values(),
        key=lambda row: (row["device"], row["assembly"], row["category"], row["name"], row["sku"]),
    )


def _excel_column(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _bom_xlsx(rows: list[dict]) -> BytesIO:
    headers = ["Изделие", "Сборочная единица", "Категория", "Поз. обозначение", "Наименование", "Артикул", "Количество"]
    data = [headers] + [[
        row.get("device") or "—",
        row.get("assembly") or "—",
        row.get("category") or "—",
        row.get("designators") or "—",
        row.get("name") or "—",
        row.get("sku") or "—",
        row.get("qty") or 0,
    ] for row in rows]

    xml_rows = []
    for row_index, values in enumerate(data, start=1):
        cells = []
        for column_index, value in enumerate(values, start=1):
            reference = f"{_excel_column(column_index)}{row_index}"
            style = ' s="1"' if row_index == 1 else ""
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                cells.append(f'<c r="{reference}"{style}><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{reference}" t="inlineStr"{style}><is><t>{escape(str(value))}</t></is></c>')
        xml_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')

    worksheet = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>
  <cols><col min="1" max="1" width="28" customWidth="1"/><col min="2" max="2" width="32" customWidth="1"/><col min="3" max="3" width="24" customWidth="1"/><col min="4" max="4" width="20" customWidth="1"/><col min="5" max="5" width="48" customWidth="1"/><col min="6" max="6" width="24" customWidth="1"/><col min="7" max="7" width="14" customWidth="1"/></cols>
  <sheetData>{''.join(xml_rows)}</sheetData>
  <autoFilter ref="A1:G{len(data)}"/>
</worksheet>'''

    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>''')
        archive.writestr("_rels/.rels", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>''')
        archive.writestr("xl/workbook.xml", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Комплектация" sheetId="1" r:id="rId1"/></sheets></workbook>''')
        archive.writestr("xl/_rels/workbook.xml.rels", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>''')
        archive.writestr("xl/styles.xml", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF3F8CFF"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs></styleSheet>''')
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
    output.seek(0)
    return output


def _parse_optional_date(value: str | None):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail="Некорректная дата поставки")
    if parsed.date() < date.today():
        raise HTTPException(
            status_code=400,
            detail="Плановая дата поставки не может быть раньше сегодняшнего дня",
        )
    return parsed


def _user_payload(user: User | None):
    if not user:
        return None
    return {
        "id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "role": user.role,
        "roles": user_roles(user),
    }


def _task_payload(task: WorkflowTask, users_by_id: dict[int, User] | None = None):
    assigned_user = users_by_id.get(task.assigned_user_id) if users_by_id and task.assigned_user_id else None
    return {
        "id": task.id,
        "type": task.type,
        "title": task.title,
        "description": task.description,
        "role": task.role,
        "status": task.status,
        "assigned_user_id": task.assigned_user_id,
        "assigned_user": _user_payload(assigned_user),
        "payload": task.payload or {},
        "created_at": task.created_at,
        "due_date": task.due_date,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
    }


def _stage_status(tasks: list[WorkflowTask]) -> str:
    if not tasks:
        return "not_created"
    if all(task.status == "done" for task in tasks):
        return "done"
    if any(task.status in ["in_progress", "ready_to_issue"] for task in tasks):
        return "in_progress"
    if any(task.status == "hold" for task in tasks):
        return "hold"
    if any(task.status in ["assigned", "open", "waiting_delivery"] for task in tasks):
        return "assigned"
    return tasks[-1].status or "assigned"


def _order_payload(order: Order, db: Session):
    tasks = (
        db.query(WorkflowTask)
        .filter(WorkflowTask.order_id == order.id)
        .order_by(WorkflowTask.created_at.asc(), WorkflowTask.id.asc())
        .all()
    )
    tasks_by_type = {}
    for task in tasks:
        tasks_by_type.setdefault(task.type, []).append(task)
    user_ids = [task.assigned_user_id for task in tasks if task.assigned_user_id]
    users_by_id = {
        user.id: user
        for user in db.query(User).filter(User.id.in_(user_ids)).all()
    } if user_ids else {}

    stages = []
    for stage in ORDER_STAGES:
        stage_tasks = []
        for task_type in stage["task_types"]:
            stage_tasks.extend(tasks_by_type.get(task_type, []))
        stage_tasks.sort(key=lambda task: (task.created_at, task.id))
        stages.append({
            "key": stage["key"],
            "title": stage["title"],
            "description": stage["description"],
            "status": _stage_status(stage_tasks),
            "tasks": [_task_payload(task, users_by_id) for task in stage_tasks],
        })

    progress = aggregate_order_progress(db, order)
    return {
        "id": order.id,
        "customer_name": order.customer_name,
        "status": progress["state"],
        "legacy_status": order.status,
        "cancellation_status": order.cancellation_status,
        "cancellation_reason": order.cancellation_reason,
        "progress": progress,
        "created_at": order.created_at,
        "planned_delivery_date": order.planned_delivery_date,
        "items": [
            {
                "id": item.id,
                "product_id": item.product_id,
                "quantity": item.quantity,
                "product": {
                    "id": item.product.id,
                    "name": item.product.name,
                    "sku": item.product.sku,
                    "drawing_number": item.product.drawing_number,
                } if item.product else None,
            }
            for item in order.items
        ],
        "tasks": [_task_payload(task, users_by_id) for task in tasks],
        "stages": stages,
        "shortages": find_order_shortages(db, order.id),
    }


@router.get("/orders", response_model=List[OrderOut], summary="Получить список всех заказов")
def get_production_orders(
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "manager", "warehouse", "production", "procurement", "assembler", "tester", "repair_engineer", "packer")),
):
    """
    Возвращает список всех заказов.
    Благодаря response_model=List[OrderOut], Pydantic автоматически трансформирует
    каждый объект, добавив внутрь позиций реальные name и sku изделий.
    """
    try:
        orders = db.query(Order).all()
        return [
            {
                "id": order.id,
                "customer_name": order.customer_name,
                "status": (progress := aggregate_order_progress(db, order))["state"],
                "legacy_status": order.status,
                "progress": progress,
                "created_at": order.created_at,
                "planned_delivery_date": order.planned_delivery_date,
                "items": order.items,
            }
            for order in orders
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка БД: {str(e)}")


@router.post("/orders", summary="Создать многопозиционный заказ")
def create_production_order(
    payload: OrderCreate,
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "manager", "production")),
):
    """
    Создает заказ для конкретного заказчика с несколькими изделиями.
    Суммирует требования BOM и резервирует компоненты.
    """
    if not payload.items:
        raise HTTPException(status_code=400, detail="Заказ должен содержать хотя бы одно изделие")

    try:
        reconcile_stock_reservations(db)
        for item in payload.items:
            if item.quantity <= 0:
                raise HTTPException(status_code=400, detail="Количество изделия должно быть больше нуля")

        new_order = Order(
            customer_name=payload.customer_name,
            status="Created",
            planned_delivery_date=_parse_optional_date(payload.planned_delivery_date),
        )
        db.add(new_order)
        db.flush()  # Получаем id созданного заказа

        order_items = []
        for item in payload.items:
            order_item = OrderItem(
                order_id=new_order.id,
                product_id=item.product_id,
                quantity=item.quantity
            )
            db.add(order_item)
            order_items.append(order_item)
        db.flush()

        all_materials = []
        product_contexts = {}
        for order_item in order_items:
            product = db.query(ProductType).filter(ProductType.id == order_item.product_id).first()
            if not product:
                raise HTTPException(status_code=404, detail=f"Изделие ID {order_item.product_id} не найдено")
            product_context = {
                "order_item_id": order_item.id,
                "product_id": product.id,
                "product_name": product.name,
                "drawing_number": product.drawing_number,
                "qty": order_item.quantity,
            }
            product_contexts[order_item.id] = product_context
            material_context = {**product_context, "product_qty": order_item.quantity}
            material_context.pop("qty", None)
            materials_list = [
                {**material, **material_context}
                for material in get_bom_requirements(order_item.product_id, order_item.quantity, db)
            ]
            all_materials.extend(materials_list)

        available_lines, all_shortages = plan_material_availability(db, all_materials)
        available_by_item = {}
        shortages_by_item = {}
        materials_by_item = {}
        for line in all_materials:
            materials_by_item.setdefault(line.get("order_item_id"), []).append(line)
        for line in available_lines:
            available_by_item.setdefault(line.get("order_item_id"), []).append(line)
        for line in all_shortages:
            shortages_by_item.setdefault(line.get("order_item_id"), []).append(line)

        for order_item in order_items:
            create_initial_order_tasks(
                db,
                new_order,
                materials_by_item.get(order_item.id, []),
                shortages_by_item.get(order_item.id, []),
                product_context=product_contexts.get(order_item.id),
                create_procurement=False,
                available_materials=available_by_item.get(order_item.id, []),
            )
        create_procurement_task_for_order(db, new_order, all_shortages)

        db.commit()
        return {
            "status": "success",
            "order_id": new_order.id,
            "order_status": new_order.status,
            "details": all_materials,
            "shortages": all_shortages,
        }

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/orders/{order_id}", summary="Детальная карточка заказа с производственной цепочкой")
def get_production_order_detail(
    order_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "manager", "warehouse", "production", "procurement", "assembler", "tester", "repair_engineer", "packer", "accounting")),
):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    reconcile_stock_reservations(db)
    db.flush()
    return _order_payload(order, db)


@router.delete("/orders/{order_id}", summary="Удалить производственный заказ")
def delete_production_order(
    order_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "manager", "production")),
):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")

    reservations = db.query(Reservation).filter(Reservation.order_id == order_id).all()
    for reservation in reservations:
        stock = db.query(Stock).filter(Stock.component_id == reservation.component_id).first()
        if stock:
            stock.reserved_qty = max((stock.reserved_qty or 0) - reservation.qty, 0)

    db.query(WorkflowTask).filter(WorkflowTask.order_id == order_id).delete(synchronize_session=False)
    db.query(Item).filter(Item.order_id == order_id).delete(synchronize_session=False)
    db.query(Reservation).filter(Reservation.order_id == order_id).delete(synchronize_session=False)
    db.query(OrderItem).filter(OrderItem.order_id == order_id).delete(synchronize_session=False)
    db.delete(order)
    db.commit()
    return {"status": "success", "detail": "Заказ удален"}


@router.post("/orders/{order_id}/issue-materials", summary="Выдача материалов под весь заказ")
def issue_materials_for_order(
    order_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "warehouse")),
):
    """
    Списывает зарезервированные материалы под все позиции этого заказа.
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    if order.status not in ["In Progress", "Reserved", "Components Available"]:
        raise HTTPException(status_code=400, detail="Материалы уже выданы или заказ завершен")

    issue_task = db.query(WorkflowTask).filter(
        WorkflowTask.order_id == order_id,
        WorkflowTask.type == "warehouse_issue_materials",
        WorkflowTask.status.in_(["in_progress", "open"]),
    ).order_by(WorkflowTask.id).first()
    if issue_task:
        try:
            result = complete_task(db, issue_task, {}, actor_user_id=_.id)
            db.commit()
            return {
                "status": "success",
                "message": "Материалы выданы, задача получения создана",
                "result": result,
            }
        except HTTPException:
            db.rollback()
            raise

    raise HTTPException(
        status_code=400,
        detail="По заказу нет активной задачи выдачи. Обновите производственный маршрут.",
    )


# =====================================================================
# ИСПРАВЛЕННЫЙ ЭНДПОИНТ: Сводная ведомость комплектующих с защитой от AttributeError
# =====================================================================
@router.get("/orders/{order_id}/bom-summary", summary="Сводная комплектация для PDF")
def get_order_bom_summary(
    order_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "manager", "warehouse", "production", "procurement", "assembler", "tester", "repair_engineer", "packer")),
):
    """
    Возвращает комплектацию заказа с сохранением структуры:
    изделие верхнего уровня -> сборочная единица -> покупные компоненты/изделия.
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")

    order_items = db.query(OrderItem).filter(OrderItem.order_id == order_id).all()
    return _structured_bom_summary_for_order(order_items, db)


@router.get("/orders/{order_id}/bom-summary.xlsx", summary="Скачать сводную комплектацию в Excel")
def download_order_bom_summary(
    order_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "manager", "warehouse", "production", "procurement", "assembler", "tester", "repair_engineer", "packer")),
):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    order_items = db.query(OrderItem).filter(OrderItem.order_id == order_id).all()
    rows = _structured_bom_summary_for_order(order_items, db)
    filename = f"Сводная комплектация заказа {order_id}.xlsx"
    return StreamingResponse(
        _bom_xlsx(rows),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@router.get("/orders/{order_id}/shortages.xlsx", summary="Скачать ведомость недостающих деталей")
def download_order_shortages(
    order_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin", "manager", "warehouse", "production", "procurement", "assembler", "tester", "repair_engineer", "packer")),
):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    shortages = find_order_shortages(db, order_id)
    rows = [[
        index,
        line.get("component_name") or f"Компонент ID {line['component_id']}",
        line.get("part_number") or "—",
        line.get("category") or "—",
        float(line.get("required_qty") or 0),
        float(line.get("available_qty") or 0),
        float(line.get("shortage_qty") or 0),
        "",
    ] for index, line in enumerate(shortages, start=1)]
    workbook = build_table_xlsx(
        sheet_name="Дефицит",
        title="Ведомость недостающих деталей",
        metadata=[("Заказ", f"№ {order_id}"), ("Заказчик", order.customer_name or "—"), ("Позиций в дефиците", str(len(shortages)))],
        headers=["№", "Наименование", "Артикул", "Категория", "Требуется", "Доступно", "Не хватает", "Примечание"],
        rows=rows,
        widths=[7, 46, 28, 24, 14, 14, 14, 30],
    )
    filename = f"Ведомость недостающих деталей заказ {order_id}.xlsx"
    return StreamingResponse(
        workbook,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )
