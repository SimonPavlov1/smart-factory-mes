import { Box, type BoxProps } from "@mui/material"

export const FormControllError = ({ children, sx, ...props }: BoxProps) => {
    return <Box {...props} sx={{
        display: "inline-flex",
        marginTop: "5px",
        color: "error.500",
        ...sx
    }}>
        {children}
    </Box>
}