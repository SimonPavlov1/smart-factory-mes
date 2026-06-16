from sqlalchemy.orm import Session
from app.database import SessionLocal, engine
from app.models.inventory import Component, Stock
from app.models.production import ProductType, ProductBOM


def populate():
    db = SessionLocal()

    # 1. Создаем компоненты
    c1 = Component(name="Резистор 10к", part_number="RES-10K", category="Резисторы")
    c2 = Component(name="Конденсатор 100нФ", part_number="CAP-100N", category="Конденсаторы")
    db.add_all([c1, c2])
    db.flush()

    # 2. Создаем остатки на складе
    s1 = Stock(component_id=c1.id, actual_qty=100.0)
    s2 = Stock(component_id=c2.id, actual_qty=50.0)
    db.add_all([s1, s2])

    # 3. Создаем изделие
    product = ProductType(name="Плата управления", drawing_number="PCB-001")
    db.add(product)
    db.flush()

    # 4. Добавляем BOM (спецификацию)
    bom1 = ProductBOM(product_id=product.id, design_name="Резистор 10к", quantity=2,
                      resource_id=c1.id, resource_type="component", is_resolved=True)
    bom2 = ProductBOM(product_id=product.id, design_name="Конденсатор 100нФ", quantity=5,
                      resource_id=c2.id, resource_type="component", is_resolved=True)
    db.add_all([bom1, bom2])

    db.commit()
    print("База данных успешно заполнена тестовыми данными!")
    db.close()


if __name__ == "__main__":
    populate()