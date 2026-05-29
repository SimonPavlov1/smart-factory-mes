import { http, HttpResponse } from "msw";

export const handlers = [
    http.get("/api/requests", () => {
        return HttpResponse.json([
            {
                "id": 1,
                "name": "Дисплей Универсальный транспортный управляющий",
                "decNum": 453891,
                "client": "ПТЗ",
                "creationDate": new Date("07.04.2026"),
                "deliveryDate": new Date("04.29.2026"),
                "priority": 0,
                "status": 0,
                "progress": 50
            },
            {
                "id": 2,
                "name": "Дисплей Универсальный транспортный управляющий",
                "decNum": 499891,
                "client": "ПТЗ",
                "creationDate": new Date("01.04.2026"),
                "deliveryDate": new Date("11.29.2026"),
                "priority": 1,
                "status": 1,
                "progress": 10
            },
            {
                "id": 3,
                "name": "Дисплей Универсальный транспортный управляющий",
                "decNum": 457691,
                "client": "ПТЗ",
                "creationDate": new Date("07.04.2026"),
                "deliveryDate": new Date("11.29.2026"),
                "priority": 2,
                "status": 2,
                "progress": 85
            }
        ]);
    }),

    http.get("/api/staff", () => {
        return HttpResponse.json([
            {
                "name": "Григорьев Алексей Иванович",
                "position": "Кладовщик",
                "phone": "7 (922)-222-22-22",
                "email": "grigAI@gmail.ru"
            },
            {
                "name": "Григорьев Иван Иванович",
                "position": "Кладовщик",
                "phone": "7 (922)-222-22-22",
                "email": "grigAI@gmail.ru"
            },
            {
                "name": "Григорьев Сергей Иванович",
                "position": "Кладовщик",
                "phone": "7 (922)-222-22-22",
                "email": "grigAI@gmail.ru"
            },
        ]);
    }),

    http.get("/api/activities", () => {
        return HttpResponse.json([
            {
                "id": 1,
                "author": "Система",
                "text": "Обнаружен дефицит по заявке №10",
                "creationDate": new Date("01.04.2026"),
                "status": "danger",
            },
            {
                "id": 2,
                "author": "Система",
                "text": "Создана новая заявка на закупку №25",
                "creationDate": new Date("07.05.2026"),
                "status": "created",
            },
            {
                "id": 3,
                "author": "Александр Пушкин",
                "text": "Принял заявку на закупку №25",
                "creationDate": new Date("02.01.2026"),
                "status": "check",
            },
            {
                "id": 4,
                "author": "Дмитрий Тарелин",
                "text": "Изменил информацию о сотруднике",
                "creationDate": new Date("07.24.2026"),
                "status": "changed",
            },
        ]);
    }),

    http.post(`/api/auth/login/`, () => {
        return HttpResponse.json({
            access: "fake-token",
            refresh: "fake-refresh"
        },
        { 
            status: 200,
            headers: {
                'Content-Type': 'application/json'
            }
        })
    })
]