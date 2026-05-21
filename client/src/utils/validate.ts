export const isValidEmail = (email: string) => {
    const pattern = /^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/i;
    return pattern.test(email);
}

export const isValidName = (name: string) => {
    const pattern = /^[a-zA-Z]{2,}$/i;
    return pattern.test(name);
}

export const isValidFullName = (name: string) => {
    const pattern = /^([a-zA-Z]{2,}) [a-zA-Z]{2,} [a-zA-Z]{2,}$/i;
    return pattern.test(name);
}