import { List } from "@mui/material"
import { ActivityItem } from "./ui/ActivityItem"
import { useEffect } from "react"

export const ActivitiesList = () => {
    useEffect(() => {
        // запрос активностей
    })
    return <List>
        <ActivityItem />
    </List>
}