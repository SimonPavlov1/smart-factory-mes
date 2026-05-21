import { clearToken, getToken, setToken } from "@utils/token";

let refreshPromise: Promise<string> | null = null;

const refreshAccessToken = () => {
    const refreshToken = getToken("refresh");

    if (!refreshToken) throw new Error("No refresh token");

    const url = import.meta.env.VITE_REFRESH_URL;

    const tokenPromise = fetch(url, {
        method: "post",
        headers: {
            "Content-Type": "application/json"
        },
        body: JSON.stringify({ refresh: refreshToken })
    });

    return tokenPromise.then((response: Response) => {
        return response.json()
            .then((payload: any) => { // Временно any
                if (!response.ok || !payload?.access) {
                    throw new Error(payload?.detail || "Refresh failed");
                }

                setToken("accessToken", payload.access);
                return payload.access;
            })
            .catch(() => {
                throw new Error("Network error");
            });
    })
}

export const fetchWrapper = async (input: RequestInfo, init?: RequestInit) => {
    const accessToken = getToken("accessToken");

    const innerRequest = (token: string | null) => {
        const headers = new Headers(init?.headers || {}); // обработка заголовков

        if (init?.body && !headers.get("Content-Type")) {
            headers.set("Content-Type", "application/json");
        }

        if (token) headers.set("Authorization", `Bearer ${token}`);

        return fetch(input, { ...init, headers });
    }

    let request = await innerRequest(accessToken);

    if (request.status === 401) {
        try {
            if (!refreshPromise) { // Обновляем токен

                refreshPromise = refreshAccessToken().finally(() => {
                    refreshPromise = null;
                });
            }

            const refreshToken = await refreshPromise;
            request = await innerRequest(refreshToken);
        } catch (error: unknown) {
            clearToken();
            window.location.href = "/auth";
            throw error;
        }
    }

    if (request.status === 204) {
        return null;
    }

    const contentType = request.headers.get("Content-Type"); // Обрабатываем тело запроса
    let data: any = null;

    if (contentType?.includes("application/json")) {
        data = await request.json().catch(() => null);
    } else {
        data = await request.text().catch(() => null);
    }

    if (!request.ok) {
        throw new Error("Error");
    }
    
    return data;
}