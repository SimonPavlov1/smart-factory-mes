import { Box, type BoxProps } from "@mui/material";

export const ListItemWrapper = ({ children, sx }: BoxProps) => {
    return <Box component="li" sx={{
        display: "flex",
        flexDirection: {
            xs: "column",
            md: "row"
        },
        justifyContent: "space-between",
        gap: "5px",
        padding: "8px 17px",
        borderRadius: "20px",
        backgroundColor: "#FFF",

        ...sx
    }}>
        {children}
    </Box>
}