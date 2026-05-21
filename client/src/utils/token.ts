export const getToken = (name: string) => {
    return localStorage.getItem(name);
}

export const setToken = (name: string, token: string) => {
    localStorage.setItem(name, token);
}

export const clearToken = () => {
    localStorage.removeItem("access");
    localStorage.removeItem("refresh");
}