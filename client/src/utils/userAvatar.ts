export const getUserAvatar = (fullName: string) => {
    const [fName, sName] = fullName.split(' ');
    return `${fName[0]}${sName[0]}`
}