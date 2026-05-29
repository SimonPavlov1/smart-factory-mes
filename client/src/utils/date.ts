import { MONTHS } from "@/consts";

export const getFullMonthDateFormat = (dateStr: string) => {
    const date = new Date(dateStr);
    const day = date.getDate() < 10 ? `0${date.getDate()}` : date.getDate();
    const month =  MONTHS[date.getMonth()];
    
    return `${day} ${month} ${date.getFullYear()}`
}

export const getLocalDateFormat = (dateStr: string) => {
    return new Date(dateStr).toLocaleDateString("ru-RU");
}
