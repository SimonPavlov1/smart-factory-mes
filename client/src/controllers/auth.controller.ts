import { clearToken } from "@utils/token";
import { fetchWrapper } from "@apis/webApi";

export const logIn = (userData: { username: string, password: string }) => {
    return fetchWrapper("/api/auth/login/", {
        method: "post",
        headers: {
            "Content-Type": "application/json"
        },
        body: JSON.stringify(userData)
    }).then((response: { "access": string, "refresh": string }) => {
        return response;
    });
}

export const logOut = () => {
    clearToken();
    window.location.href = "/auth";
}