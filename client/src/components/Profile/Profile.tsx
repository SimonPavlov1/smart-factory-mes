import { Box, FormControl, Input, InputLabel, Typography } from "@mui/material";
import { DatePicker } from "@mui/x-date-pickers";
import { UserAvatar } from "@ui/UserAvatar/UserAvatar";
import { useState } from "react";

export const Profile = () => {
    const [isOpen, setIsOpen] = useState(false);
    return <Box sx={{
        padding: "10px",
        maxWidth: {
                xs: "fit-content",
                sm: "210px"
            },
        width: "100%",
        position: "relative",
        backgroundColor: "#fff",
        borderRadius: "10px",
    }}>
        <Box onClick={() => setIsOpen(!isOpen)} sx={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: "10px",
        }}>
            <UserAvatar size="sm">И</UserAvatar>
            <Box sx={{
                display: {
                    xs: "none",
                    sm: "block"
                }
            }}>
                <Box sx={{
                    fontWeight: 700,
                    fontSize: "1.6rem"
                }}>Иванов Иван</Box>
                <Box sx={{
                    fontWeight: 700,
                    fontSize: "0.8rem",
                    color: "grey.500"
                }}>Начальник производства</Box>
            </Box>
        </Box>
        {isOpen ? <Box sx={{ 
            position: "absolute", 
            top: "110%", 
            right: "25%", 
            borderRadius: "24px",
            backgroundColor: "#fff", 
            boxShadow: "0 0 15px rgba(0, 0, 0, .2)",
            zIndex: 999 }}>
            <Box sx={{
                padding: "28px 24px",
                borderBottom: "1px solid #E4E6E8"
            }}>
                <UserAvatar size="md">И</UserAvatar>
                <Box sx={{
                    fontWeight: 700,
                    fontSize: "2.2rem"
                }}>Иванов Иван</Box>
                <Box sx={{
                    fontWeight: 700,
                    fontSize: "1.4rem",
                    color: "grey.500"
                }}>Начальник производства</Box>
            </Box>
            
            <Box sx={{
                padding: "28px 24px"
            }}>
                <Box component="section">
                    <Typography variant="h3" sx={{ marginBottom: "12px" }}>Основная информация</Typography>
                    <FormControl sx={{ width: "100%" }}>
                        <InputLabel>Должность</InputLabel>
                        <Input disableUnderline disabled />
                    </FormControl>
                    <FormControl sx={{ marginTop: "16px", width: "100%" }}>
                        <InputLabel>Дата рождения</InputLabel>
                        <DatePicker disabled />
                    </FormControl>
                </Box>
                <Box component="section" sx={{ marginTop: "28px" }}>
                    <Typography variant="h3" sx={{ marginBottom: "12px" }}>Контактная информация</Typography>
                    <FormControl sx={{ width: "100%" }}>
                        <InputLabel>Email</InputLabel>
                        <Input disableUnderline disabled />
                    </FormControl>
                    <FormControl sx={{ marginTop: "16px", width: "100%" }}>
                        <InputLabel>Телефон</InputLabel>
                        <Input disableUnderline disabled />
                    </FormControl>
                </Box>
            </Box>
        </Box> : null}
    </Box>
}