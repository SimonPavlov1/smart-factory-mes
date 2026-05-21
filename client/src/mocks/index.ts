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
                "deliveryDate": new Date("29.04.2026"),
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
                "deliveryDate": new Date("29.09.2026"),
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
                "deliveryDate": new Date("29.11.2026"),
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