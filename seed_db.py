from app.database import SessionLocal
from app.models.inventory import Component, Stock
from app.models.product import ProductType, ProductBOM


def seed():
    db = SessionLocal()

    # 1. Добавляем компоненты
    resistor = Component(
        category="Resistors",
        value="10k",
        package="0805",
        part_number="RES-10K-0805-1%"
    )
    mcu = Component(
        category="IC",
        value="STM32F103",
        package="LQFP48",
        part_number="STM32F103C8T6"
    )

    db.add_all([resistor, mcu])
    db.commit()  # Сохраняем, чтобы получить ID

    # 2. Выставляем остатки на склад
    stock_res = Stock(component_id=resistor.id, actual_qty=1000, location="A-1-1")
    stock_mcu = Stock(component_id=mcu.id, actual_qty=50, location="B-2-4")

    db.add_all([stock_res, stock_mcu])

    # 3. Создаем тип изделия (например, "Датчик температуры")
    sensor = ProductType(name="Smart Temp Sensor", decimal_number="ST-001-REV1")
    db.add(sensor)
    db.commit()

    # 4. Прописываем состав (BOM) — для одного датчика нужно 4 резистора и 1 МС
    bom1 = ProductBOM(product_id=sensor.id, component_id=resistor.id, quantity=4, designator="R1-R4")
    bom2 = ProductBOM(product_id=sensor.id, component_id=mcu.id, quantity=1, designator="U1")

    db.add_all([bom1, bom2])
    db.commit()
    db.close()
    print("Тестовые данные успешно загружены!")


if __name__ == "__main__":
    seed()