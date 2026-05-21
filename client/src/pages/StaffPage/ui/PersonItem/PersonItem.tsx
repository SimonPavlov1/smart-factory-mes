import { Box } from "@mui/material";
import "./personItem.css";
import { UserAvatar } from "@ui/UserAvatar/UserAvatar";
import { ListItemWrapper } from "@ui/ListItemWrapper/ListItemWrapper";
import type { PersonProps } from "@models/person.model";
import { getUserAvatar } from "@utils/userAvatar";

export const PersonItem = ({ person }: { person: PersonProps }) => {
    return <ListItemWrapper sx={{
        "&:nth-child(1n+2)": {
            marginTop: "35.66px"
        }
    }}>
        <Box className="person-item__group">
            <UserAvatar>{
                person.avatar 
                ? <img src={person.avatar} />
                : getUserAvatar(person.name)
                }</UserAvatar>
            <Box className="person-item__col">
                <Box className="person-item__col-header">ФИО</Box>
                <Box className="person-item__col-content" sx={{ width: "min-content" }}>{person.name}</Box>
            </Box>
        </Box>
        <Box className="person-item__col">
            <Box className="person-item__col-header">Должность</Box>
            <Box className="person-item__col-content">{person.position}</Box>
        </Box>
        <Box className="person-item__col">
            <Box className="person-item__col-header">Телефон</Box>
            <Box className="person-item__col-content">{person.phone}</Box>
        </Box>
        <Box className="person-item__col">
            <Box className="person-item__col-header">Email</Box>
            <Box className="person-item__col-content">{person.email}</Box>
        </Box>
    </ListItemWrapper>
}