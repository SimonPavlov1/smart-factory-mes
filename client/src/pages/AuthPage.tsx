import { FormControllError } from "@/ui/FormControllError/FormControllError";
import { Box, Button, FormGroup, Input, Typography } from "@mui/material";
import { logIn } from "@controllers/auth.controller";
import React, { useState } from "react";
import { setToken } from "@utils/token";

export const AuthPage = () => {
    const [username, setUsername] = useState("");
    const [usernameError, setUsernameError] = useState("");

    const [password, setPassword] = useState("");
    const [passwordError, setPasswordError] = useState("");

    return <Box component="section" sx={{
        height: "100vh",
        padding: "81px 10px",
        color: "#FFF",
        bgcolor: "primary.main",
        textAlign: "center"
    }}>
        <Typography variant="h1" sx={{
            fontSize: "4.2rem",
            fontWeight: "700"
        }}>Авторизация</Typography>

        <Typography variant="body1" sx={{
            margin: "10px 0",
            fontSize: "2.4rem",
            fontWeight: "300"
        }}>Для доступа к личному кабинету управления проектами, пожалуйста, введите
            свои данные.</Typography>

        <Box component="form" onSubmit={(event: React.FormEvent<HTMLFormElement>) => {
            event.preventDefault();
            logIn({ username, password })
            .then((res) => {
                setToken("access", res.refresh);
                setToken("refresh", res.refresh);
                window.location.href = "/";
            })
            .catch(() => {
                setPasswordError("Error password");
                setUsernameError("Error username");
            });
        }}>
            <FormGroup row={true} sx={{
                margin: "0 auto",
                maxWidth: "779px",
                gap: {
                    xs: "8px",
                    sm: "24px"
                },
                justifyContent: "center",
                marginBottom: "10px",
                flexDirection: {
                    xs: "column",
                    sm: "row"
                }
            }}>
                <Box sx={{ flex: 1}}>
                    <Input sx={{ width: "100%", fontSize: "1.6rem" }} name="username" value={username} onChange={(ev) => setUsername(ev.currentTarget.value)} required placeholder="Имя пользователя" />
                    <FormControllError>{usernameError}</FormControllError>
                </Box>
                <Box sx={{ flex: 1}}>
                    <Input sx={{ width: "100%", fontSize: "1.6rem" }} type="password" name="password" value={password} onChange={(ev) => setPassword(ev.currentTarget.value)} required placeholder="Пароль" />
                    <FormControllError>{passwordError}</FormControllError>
                </Box>
            </FormGroup>

            <Button sx={{
                padding: "2rem 5.8rem",
                borderRadius: "14px",
                color: "#000",
                backgroundColor: "#FFF",
                fontWeight: "700",
                fontSize: "1.6rem"
            }} type="submit">Войти</Button>
        </Box>
    </Box>
}