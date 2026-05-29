import { Box, Typography } from "@mui/material";
import DangerIcon from "@icons/danger.svg?react";
import CheckMark from "@icons/checkMark.svg?react";
import MoneyIcon from "@icons/money.svg?react";
import RocketIcon from "@icons/rocket.svg?react";
import UsersIcon from "@icons/users.svg?react";
import { UserAvatar } from "@ui/UserAvatar/UserAvatar";
import { Link } from "react-router";
import type { ActivityProps } from "@models/activity.model";
import { getUserAvatar } from "@utils/userAvatar";
import { getFullMonthDateFormat } from "@utils/date";

const ACTIVITIES_ICONS: {
    [key: string]: React.FunctionComponent<React.SVGProps<SVGSVGElement> & {
        title?: string;
        titleId?: string;
        desc?: string;
        descId?: string;
    }>
} = {
    danger: DangerIcon,
    check: CheckMark,
    created: MoneyIcon,
    changed: UsersIcon
}

export const ActivityItem = ({ item }: { item: ActivityProps }) => {
    const ActivityIcon = ACTIVITIES_ICONS[item.status];
    return <Box component="li" sx={{
        padding: "9px 14px",
        borderRadius: "14px",
        backgroundColor: "#F4F9FD",

        "&:nth-child(1n+2)": {
            marginTop: "10px"
        }
    }}>
        <Box sx={{ display: "flex", gap: "10px", alignItems: "center" }}>
            {item.author === "Система"
                ? <>
                    <UserAvatar size="xs">
                        <RocketIcon />
                    </UserAvatar>
                    <Typography variant="h3" sx={{ fontSize: "1rem" }}>{item.author}</Typography>
                </>
                : <>
                    <UserAvatar size="xs">
                        {getUserAvatar(item.author)}
                    </UserAvatar>
                    <Typography variant="h3" sx={{ fontSize: "1rem" }}>{item.author}</Typography>
                </>}
        </Box>
        <Box sx={[
            {
                display: "flex",
                gap: "10px",
                alignItems: "center",
                marginTop: "10px",
                marginBottom: "3px",
            },
            item.status !== "danger" && {
                "& path": {
                    fill: (theme) => theme.palette.primary.main
                }
            }
        ]
        }>
            <ActivityIcon />
            {item.text}
        </Box>
        <Box sx={{
            display: "flex",
            justifyContent: "space-between"
        }}>
            <Link to={"/"}>Просмотреть</Link>
            <Typography variant="body1">{getFullMonthDateFormat(item.creationDate)}</Typography>
        </Box>
    </Box>
}