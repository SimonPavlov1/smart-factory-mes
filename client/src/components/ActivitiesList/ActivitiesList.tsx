import { List } from "@mui/material";
import { ActivityItem } from "./ui/ActivityItem";
import { useEffect, useState } from "react";
import type { ActivityProps } from "@models/activity.model";
import { fetchWrapper } from "@apis/webApi";

export const ActivitiesList = () => {
    const [activities, setActivities] = useState([]);
    useEffect(() => {
        fetchWrapper("/api/activities")
        .then((response) => {
            setActivities(response);
        })
    }, [])
    return <List>
        {activities.length === 0 ? null : activities.map((item: ActivityProps) => <ActivityItem item={item} key={item.author + item.id} />)}
    </List>
}