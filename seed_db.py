from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models.inventory import Component
from app.models.production import ProductType, ProductBOM

def seed():
    db: Session = SessionLocal()
    print("Наполнение базы данными РСДТ.687281.011 (используем part_number)...")

    try:
        # 1. Создаем компоненты
        components_data = [
            {"name": "Плата печатная", "pn": "РСДТ.758723.010", "cat": "PCB"},
            {"name": "Разъем СКК7353NS-1.5-118", "pn": "KINKONG-SKK", "cat": "Connector"},
            {"name": "Разъем I-DS1070-SCW004", "pn": "Connfly-SCW004", "cat": "Connector"},
            {"name": "Разъем I-DS1070-SCW006", "pn": "Connfly-SCW006", "cat": "Connector"},
            {"name": "Разъем XF2M-4015-1A", "pn": "Omron-XF2M", "cat": "Connector"},
            {"name": "Провод ПВАМ-0,5 белый", "pn": "PVAM-0.5-W", "cat": "Wire"},
        ]

        created_components = {}
        for item in components_data:
            comp = Component(
                name=item["name"],
                part_number=item["pn"],
                category=item["cat"]
            )
            db.add(comp)
            db.flush()
            created_components[item["pn"]] = comp.id

        # 2. Создаем изделие
        product_a1 = ProductType(
            name="Плата А1",
            sku="A1-BOARD",
            drawing_number="РСДТ.687281.011",
            is_subassembly=True,
            description="Спецификация из файла РСДТ.687281.011"
        )
        db.add(product_a1)
        db.flush()

        # 3. Наполняем BOM данными
        bom_items = [
            {"res_id": created_components["РСДТ.758723.010"], "qty": 1.0, "des": None},
            {"res_id": created_components["KINKONG-SKK"], "qty": 1.0, "des": "X100"},
            {"res_id": created_components["Connfly-SCW004"], "qty": 1.0, "des": "X30"},
            {"res_id": created_components["Connfly-SCW006"], "qty": 1.0, "des": "X2"},
            {"res_id": created_components["Omron-XF2M"], "qty": 1.0, "des": "X1"},
            {"res_id": created_components["PVAM-0.5-W"], "qty": 2.0, "des": "м"},
        ]

        for item in bom_items:
            db.add(ProductBOM(
                product_id=product_a1.id,
                resource_id=item["res_id"],
                resource_type="component",
                quantity=item["qty"],
                designators=item["des"]
            ))

        db.commit()
        print("База успешно наполнена!")
    except Exception as e:
        db.rollback()
        print(f"Ошибка при наполнении: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    seed()