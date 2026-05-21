import "./header.css";
import type React from "react";
import { Profile } from "@/components/Profile/Profile";
import { SearchBar } from "@/ui/SearchBar/SearchBar";
import { Box } from "@mui/material";

export const Header = ({ children, ...props }: React.ComponentProps<'header'>) => {
    return <header {...props}>
        <Box className="header-row" sx={{ gap: "10px" }}>
            <SearchBar />
            <Profile />
        </Box>
        {children}
    </header>
}