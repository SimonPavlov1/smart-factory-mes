import "./navMenu.css";
import { Button, Box } from "@mui/material";
import { MenuLink } from "./ui/MenuLink";
import DashboardIcon from "@icons/dashboard.svg?react";
import UsersIcon from "@icons/users.svg?react";
import ProjectsIcon from "@icons/projects.svg?react";
import LogoutIcon from "@icons/logout.svg?react";
import { clearToken } from "@/utils/token";

export const NavMenu = () => {
    return <Box className="menu">
        <img src="/Logo.svg" />
        <nav className="menu__nav">
            <MenuLink to={{ pathname: "/" }} icon={DashboardIcon}>Панель</MenuLink>
            <MenuLink to={{ pathname: "/requests" }} icon={ProjectsIcon}>Все заявки</MenuLink>
            <MenuLink to={{ pathname: "/staff" }} icon={UsersIcon}>Персонал</MenuLink>
        </nav>
        <Button sx={{
            marginTop: "auto",
            padding: 0,
            paddingLeft: "11px",
            justifyContent: "left",
            gap: "10px",
            fontSize: "min(2.2vw, 1.7rem)",
            backgroundColor: "transparent",
            color: "grey.900"
        }} onClick={() => {
            clearToken();
            window.location.href = "/auth";
        }}>
            <LogoutIcon />
            Выход
        </Button>
    </Box>
}