from fastapi import FastAPI, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models.inventory import Component, Stock
from app.models.production import ProductType, ProductBOM, Order
from app.schemas.procurement import (
    PurchaseDeliveryUpdate,
    PurchaseOrderCreate,
    PurchaseOrderDetail,
    PurchaseOrderRead,
    PurchaseReceiveRequest,
)
from app.services import procurement as procurement_service

app = FastAPI(
    title="Smart Factory MES API",
    description="Система управления производственными процессами и складским учетом (MES)",
    version="1.0.0"
)

# --- Dependency ---

def get_db():
    """Инициализация сессии базы данных для каждого запроса."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --- 1. Inventory Management (Склад и НСИ) ---

@app.get("/components", tags=["Inventory"])
def get_components(db: Session = Depends(get_db)):
    """
    Получить полный справочник ТМЦ.
    """
    return db.query(Component).all()


@app.get("/components/{component_id}", tags=["Inventory"])
def get_component_by_id(component_id: int, db: Session = Depends(get_db)):
    """
    Получить детальную информацию о конкретном компоненте.
    """
    component = db.query(Component).get(component_id)
    if not component:
        raise HTTPException(status_code=404, detail="Компонент не найден")
    return component


@app.post("/inventory/incoming", tags=["Inventory"])
def add_stock(component_id: int, quantity: float, location: str = "Warehouse-1", db: Session = Depends(get_db)):
    """
    Оприходование ТМЦ на склад (увеличение actual_qty).
    """
    stock_item = db.query(Stock).filter(Stock.component_id == component_id).first()

    if stock_item:
        stock_item.actual_qty += quantity
        stock_item.location = location
    else:
        stock_item = Stock(component_id=component_id, actual_qty=quantity, location=location)
        db.add(stock_item)

    db.commit()
    return {"status": "Success", "total_on_hand": stock_item.actual_qty}


# --- 2. Production Engineering (Инжиниринг) ---

@app.get("/products/{product_id}/bom", tags=["Engineering"])
def get_product_bom(product_id: int, db: Session = Depends(get_db)):
    """
    Получить конструкторский состав изделия (Bill of Materials).
    """
    product = db.query(ProductType).get(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Изделие не найдено")

    bom_items = db.query(ProductBOM).filter(ProductBOM.product_id == product_id).all()

    result = []
    for item in bom_items:
        comp = db.query(Component).get(item.resource_id)
        result.append({
            "designator": item.designators,
            "quantity": item.quantity,
            "component_name": comp.name if comp else "Unknown",
            "part_number": comp.part_number if comp else "N/A"
        })

    return {
        "product_name": product.name,
        "drawing_number": product.drawing_number,
        "bill_of_materials": result
    }


# --- 3. Production Control (Управление производством) ---

@app.post("/orders", status_code=status.HTTP_201_CREATED, tags=["Production"])
def create_production_order(product_id: int, quantity: int, db: Session = Depends(get_db)):
    """
    Регистрация нового производственного задания (Статус: New).
    """
    product = db.query(ProductType).get(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Продукт не зарегистрирован")

    new_order = Order(product_id=product_id, target_qty=quantity, status="New")
    db.add(new_order)
    db.commit()
    db.refresh(new_order)
    return new_order


@app.get("/orders/{order_id}/mrp", tags=["Production"])
def check_order_mrp(order_id: int, db: Session = Depends(get_db)):
    """
    MRP-анализ: Расчет дефицита материалов под конкретный заказ.
    """
    order = db.query(Order).get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")

    bom_items = db.query(ProductBOM).filter(ProductBOM.product_id == order.product_id).all()

    report = []
    for item in bom_items:
        comp = db.query(Component).get(item.resource_id)
        stock = db.query(Stock).filter(Stock.component_id == item.resource_id).first()

        current_stock = stock.actual_qty if stock else 0.0
        required_total = item.quantity * order.target_qty
        shortage = max(0.0, required_total - current_stock)

        report.append({
            "component": comp.name if comp else "Unknown",
            "required": required_total,
            "available": current_stock,
            "shortage": shortage,
            "status": "OK" if shortage <= 0 else "SHORTAGE"
        })

    return {
        "order_id": order_id,
        "can_start": all(i["status"] == "OK" for i in report),
        "materials_status": report
    }


@app.post("/orders/{order_id}/start", tags=["Production"])
def start_production(order_id: int, db: Session = Depends(get_db)):
    """
    Запуск заказа в производство: проверка остатков и автоматическое списание.
    """
    order = db.query(Order).get(order_id)
    if not order or order.status != "New":
        raise HTTPException(status_code=400, detail="Заказ не найден или уже запущен")

    bom_items = db.query(ProductBOM).filter(ProductBOM.product_id == order.product_id).all()

    # Валидация остатков (защита от запуска при дефиците)
    for item in bom_items:
        stock = db.query(Stock).filter(Stock.component_id == item.resource_id).first()
        required_qty = item.quantity * order.target_qty
        if not stock or stock.actual_qty < required_qty:
            raise HTTPException(
                status_code=400,
                detail=f"Дефицит: {comp.name if (comp := db.query(Component).get(item.resource_id)) else item.resource_id}"
            )

    # Процесс списания
    for item in bom_items:
        stock = db.query(Stock).filter(Stock.component_id == item.resource_id).first()
        stock.actual_qty -= (item.quantity * order.target_qty)

    order.status = "In Progress"
    db.commit()
    return {"message": "Order started, inventory deducted", "new_status": order.status}


# --- 4. Procurement (Supply Management) ---

def _purchase_order_detail(db: Session, purchase_id: int) -> dict:
    purchase_order = procurement_service.get_purchase_order(db, purchase_id)
    if not purchase_order:
        raise HTTPException(status_code=404, detail="Purchase order not found")

    items = procurement_service.list_purchase_items(db, purchase_id)
    return {
        "id": purchase_order.id,
        "supplier_name": purchase_order.supplier_name,
        "status": purchase_order.status,
        "invoice_ref": purchase_order.invoice_ref,
        "tracking_code": purchase_order.tracking_code,
        "arrival_date": purchase_order.arrival_date,
        "items": items,
    }


@app.post(
    "/procurement/orders",
    status_code=status.HTTP_201_CREATED,
    response_model=PurchaseOrderDetail,
    tags=["Procurement"],
)
def create_purchase_order(payload: PurchaseOrderCreate, db: Session = Depends(get_db)):
    component_ids = {item.component_id for item in payload.items}
    existing_components = (
        db.query(Component.id)
        .filter(Component.id.in_(component_ids))
        .all()
    )
    existing_ids = {row[0] for row in existing_components}
    missing_ids = sorted(component_ids - existing_ids)

    if missing_ids:
        raise HTTPException(
            status_code=404,
            detail=f"Components not found: {missing_ids}",
        )

    purchase_order = procurement_service.create_purchase_order(
        db=db,
        supplier_name=payload.supplier_name,
        items=[item.model_dump() for item in payload.items],
    )
    return _purchase_order_detail(db, purchase_order.id)


@app.get(
    "/procurement/orders",
    response_model=list[PurchaseOrderRead],
    tags=["Procurement"],
)
def list_purchase_orders(
    status: str | None = None,
    supplier_name: str | None = None,
    db: Session = Depends(get_db),
):
    return procurement_service.list_purchase_orders(
        db=db,
        status=status,
        supplier_name=supplier_name,
    )


@app.get(
    "/procurement/orders/{purchase_id}",
    response_model=PurchaseOrderDetail,
    tags=["Procurement"],
)
def get_purchase_order(purchase_id: int, db: Session = Depends(get_db)):
    return _purchase_order_detail(db, purchase_id)


@app.patch(
    "/procurement/orders/{purchase_id}",
    response_model=PurchaseOrderDetail,
    tags=["Procurement"],
)
def update_purchase_order_delivery(
    purchase_id: int,
    payload: PurchaseDeliveryUpdate,
    db: Session = Depends(get_db),
):
    purchase_order = procurement_service.update_purchase_delivery(
        db=db,
        purchase_id=purchase_id,
        **payload.model_dump(exclude_unset=True),
    )

    if not purchase_order:
        raise HTTPException(status_code=404, detail="Purchase order not found")

    return _purchase_order_detail(db, purchase_order.id)


@app.post(
    "/procurement/orders/{purchase_id}/receive",
    response_model=PurchaseOrderDetail,
    tags=["Procurement"],
)
def receive_purchase_order(
    purchase_id: int,
    payload: PurchaseReceiveRequest | None = None,
    db: Session = Depends(get_db),
):
    purchase_order = procurement_service.get_purchase_order(db, purchase_id)
    if not purchase_order:
        raise HTTPException(status_code=404, detail="Purchase order not found")

    if purchase_order.status == "Received":
        raise HTTPException(status_code=400, detail="Purchase order already received")

    receive_payload = payload or PurchaseReceiveRequest()
    purchase_order = procurement_service.receive_purchase_order(
        db=db,
        purchase_id=purchase_id,
        location=receive_payload.location,
    )
    return _purchase_order_detail(db, purchase_order.id)
