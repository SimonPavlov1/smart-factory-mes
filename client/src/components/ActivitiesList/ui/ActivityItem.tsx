import { Box, Typography } from "@mui/material";
import DangerIcon from "@icons/danger.svg?react";
import CheckMark from "@icons/checkMark.svg?react";
import MoneyIcon from "@icons/money.svg?react";
import RocketIcon from "@icons/rocket.svg?react";
import { UserAvatar } from "@ui/UserAvatar/UserAvatar";
import { useState } from "react";
import { Link } from "react-router";

const ACTIVITIES_ICONS = {
    danger: DangerIcon,
    check: CheckMark,
    money: MoneyIcon
}

export const ActivityItem = () => {
    const [isSystem, setIsSystem] = useState(true);
    const ActivityIcon = ACTIVITIES_ICONS["danger"];
    return <Box component="li" sx={{
        padding: "9px 14px",
        borderRadius: "14px",
        backgroundColor: "#F4F9FD"
    }}>
        <Box sx={{ display: "flex", gap: "10px", alignItems: "center" }}>
            {isSystem
                ? <>
                    <UserAvatar size="xs">
                        <RocketIcon />
                    </UserAvatar>
                    <Typography variant="h3" sx={{ fontSize: "1rem" }}>Система</Typography>
                </>
                : <>
                    <UserAvatar size="xs">
                        ДТ
                    </UserAvatar>
                    <Typography variant="h3" sx={{ fontSize: "1rem" }}>Дмитрий Тарелин</Typography>
                </>}
        </Box>
        <Box sx={{ display: "flex", gap: "10px", alignItems: "center", marginTop: "10px", marginBottom: "3px"}}>
            <ActivityIcon />
            Обнаружен дефицит по заявке №10
        </Box>
        <Box sx={{
            display: "flex",
            justifyContent: "space-between"
        }}>
            <Link to={"/"}>Просмотреть</Link>
            <Typography variant="body1">19 июня 2025</Typography>
        </Box>
    </Box>
}