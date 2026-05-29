import "./RequestItem.css";
import { Box, CircularProgress } from "@mui/material";
import PriorityIcon from "@icons/priority.svg?react";
import { ListItemWrapper } from "@ui/ListItemWrapper/ListItemWrapper";
import type { RequestProps } from "@/models/request.model";
import { getLocalDateFormat } from "@utils/date";
import { PRIORITIES, STATUSES } from "@/consts";

const PRIORITIES_CLASSES: { [key: number]: string } = {
    0: "low",
    1: "md",
    2: "high"
}

const STATUSES_CLASSES: { [key: number]: string } = {
    0: "request-item__status--created",
}

export const RequestItem = ({ item }: { item: RequestProps }) => {
    return <ListItemWrapper sx={{
        "&:nth-child(1n+2)": {
            marginTop: "21.44px"
        }
    }}>
            <Box className="request-item__col">
                <Box className="request-item__col-header">ID</Box>
                <Box className="request-item__col-content">{item.id}</Box>
            </Box>
            <Box className="request-item__col">
                <Box className="request-item__col-header">Наименование</Box>
                <Box className="request-item__col-content">{item.name}</Box>
            </Box>
            <Box className="request-item__col">
                <Box className="request-item__col-header">Децимальный №</Box>
                <Box className="request-item__col-content">{item.decNum}</Box>
            </Box>
            <Box className="request-item__col">
                <Box className="request-item__col-header">Заказчик</Box>
                <Box className="request-item__col-content">{item.client}</Box>
            </Box>
            <Box className="request-item__col">
                <Box className="request-item__col-header">Дата создания</Box>
                <Box className="request-item__col-content">{getLocalDateFormat(item.creationDate)}</Box>
            </Box>
            <Box className="request-item__col">
                <Box className="request-item__col-header">Дата поставки</Box>
                <Box className="request-item__col-content">{getLocalDateFormat(item.deliveryDate)}</Box>
            </Box>
            <Box className="request-item__col">
                <Box className="request-item__col-header">Приоритет</Box>
                <Box className={`request-item__col-content request-item__priority request-item__priority--${PRIORITIES_CLASSES[item.priority]}`}>
                    <PriorityIcon />
                    {PRIORITIES[item.priority]}
                </Box>
            </Box>
            <Box className="request-item__col">
                <Box className={`request-item__status ${STATUSES_CLASSES[item.status] || ""}`}>{STATUSES[item.status]}</Box>
            </Box>
            <Box className="request-item__col">
                <CircularProgress variant="determinate" value={item.progress} aria-label="Request progress" enableTrackSlot />
            </Box>
        </ListItemWrapper>
}