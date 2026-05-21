import { Box, Input } from "@mui/material";
import SearchIcon from "@icons/search.svg?react";

export const SearchBar = () => {
    return <Box sx={{
        display: "flex",
        alignItems: "center",
        gap: "9px",
        maxWidth: "348px",
        width: "100%",
        padding: "10px 16px",
        borderRadius: "12px",
        backgroundColor: "#fff"
    }}>
        <SearchIcon />
        <Input placeholder="Поиск" disableUnderline sx={{
            margin: 0,
            padding: 0,
            flex: 1,
            fontSize: "1.35rem",
            border: "none",
            ":hover": {
                border: "none"
            }
        }} />
    </Box>
};