export const getDate = (dateStr: string) => {
    const date = new Date(dateStr);
    return `${date.getDate()}.${date.getMonth()}.${date.getFullYear()}`
}
