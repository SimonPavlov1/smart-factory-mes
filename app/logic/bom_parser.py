import re
from typing import Dict, Any, Optional


def parse_with_context(design_name: str, category_context: Optional[str] = None) -> Dict[str, Any]:
    """
    Универсальный парсер для извлечения технических характеристик из текстового описания детали.

    Args:
        design_name: Текст из графы "Наименование" (напр. 'Резистор 0603 10к 1%').
        category_context: Заголовок раздела из ПЭ3 (напр. 'Конденсаторы').

    Returns:
        Словарь с нормализованными данными (категория, корпус, номинал в СИ и т.д.).
    """
    if not design_name:
        return {}

    # Структура ответа по умолчанию
    res = {
        "category": category_context.strip() if category_context else "Other",
        "package": None,  # Типоразмер (0603, SOT-23)
        "value_numeric": None,  # Числовое значение (10000.0)
        "value_unit": None,  # Единица измерения (Ohm, F, H)
        "voltage": None,  # Напряжение (V)
        "tolerance": None,  # Допуск (%, X7R)
        "raw_text": design_name.strip()
    }

    text = res["raw_text"]

    # -------------------------------------------------------------------------
    # ШАГ 1: Поиск типоразмера (Package)
    # -------------------------------------------------------------------------
    # Ищем стандартные паттерны SMD корпусов (4 цифры) или специзделий
    pkg_pattern = r'\b(0201|0402|0603|0805|1206|1210|2010|2512|SOT-23|SOT-223|SOT-89|DPAK|D2PAK|TO-220|SMA|SMB|SMC|LQFP\d*|QFN\d*|TSSOP\d*)\b'
    pkg_match = re.search(pkg_pattern, text, re.I)
    if pkg_match:
        res["package"] = pkg_match.group(1).upper()

    # -------------------------------------------------------------------------
    # ШАГ 2: Специализированная экстракция на основе категории
    # -------------------------------------------------------------------------
    cat_upper = res["category"].upper()

    # --- КОНДЕНСАТОРЫ (Обработка емкости и диэлектрика) ---
    if "КОНДЕНСАТОР" in cat_upper:
        res["category"] = "Capacitors"
        # Ищем емкость: число + (пФ, нФ, мкФ или латиница)
        cap_match = re.search(r'(\d+[.,]?\d*)\s?(pF|nF|uF|мкФ|нФ|пФ|u|n|p)', text, re.I)
        if cap_match:
            val = float(cap_match.group(1).replace(',', '.'))
            unit = cap_match.group(2).lower()
            multipliers = {
                'p': 1e-12, 'пф': 1e-12,
                'n': 1e-9, 'нф': 1e-9,
                'u': 1e-6, 'мкф': 1e-6,
                'm': 1e-3, 'мф': 1e-3
            }
            res["value_numeric"] = val * multipliers.get(unit, 1)
            res["value_unit"] = "F"

        # Напряжение (напр. 50V, 100В)
        volt_match = re.search(r'(\d+[.,]?\d*)\s?(?:V|В)', text, re.I)
        if volt_match:
            res["voltage"] = float(volt_match.group(1).replace(',', '.'))

    # --- РЕЗИСТОРЫ (Обработка сопротивления) ---
    elif "РЕЗИСТОР" in cat_upper:
        res["category"] = "Resistors"
        # Ищем сопротивление: число + (R, k, M, Ом)
        res_match = re.search(r'(\d+[.,]?\d*)\s?(R|k|M|кОм|МОм|Ом)', text, re.I)
        if res_match:
            val = float(res_match.group(1).replace(',', '.'))
            unit = res_match.group(2).lower()
            multipliers = {
                'r': 1, 'ом': 1,
                'k': 1e3, 'ком': 1e3,
                'm': 1e6, 'мом': 1e6
            }
            res["value_numeric"] = val * multipliers.get(unit, 1)
            res["value_unit"] = "Ohm"

    # --- ПОЛУПРОВОДНИКИ (Диоды, Стабилитроны, Транзисторы) ---
    elif any(word in cat_upper for word in ["ДИОД", "СТАБИЛИТРОН", "ТРАНЗИСТОР"]):
        if "ДИОД" in cat_upper:
            res["category"] = "Diodes"
        elif "СТАБИЛИТРОН" in cat_upper:
            res["category"] = "Zener Diodes"
        else:
            res["category"] = "Transistors"

        # Для полупроводников важно напряжение (пробоя/стабилизации)
        volt_match = re.search(r'(\d+[.,]?\d*)\s?(?:V|В)', text, re.I)
        if volt_match:
            res["voltage"] = float(volt_match.group(1).replace(',', '.'))

    # --- МИКРОСХЕМЫ (Integrated Circuits) ---
    elif "МИКРОСХЕМА" in cat_upper:
        res["category"] = "Integrated Circuits"
        # Для микросхем параметры индивидуальны, полагаемся на поиск по MPN

    # --- FALLBACK (Любые другие типы: разъемы, кнопки, модули) ---
    else:
        # Если категория не опознана, пытаемся найти вольтаж как универсальный параметр
        volt_match = re.search(r'(\d+[.,]?\d*)\s?(?:V|В)', text, re.I)
        if volt_match:
            res["voltage"] = float(volt_match.group(1).replace(',', '.'))

    # -------------------------------------------------------------------------
    # ШАГ 3: Универсальный поиск допуска (Tolerance)
    # -------------------------------------------------------------------------
    # Работает для всех типов: 1%, X7R, NP0 и т.д.
    tol_match = re.search(r'\b(X7R|X5R|NP0|C0G|Y5V|0\.1%|1%|5%|10%)\b', text, re.I)
    if tol_match:
        res["tolerance"] = tol_match.group(1).upper()

    return res