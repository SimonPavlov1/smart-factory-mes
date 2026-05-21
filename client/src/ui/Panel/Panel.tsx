import { Box, Typography, type BoxProps } from "@mui/material";

type PanelProps = BoxProps & {
    headerText: string,
    subtitleText?: string
}

export const Panel = ({ children, headerText, subtitleText, ...props }: PanelProps) => {
    return <Box component="section" {...props} sx={{ 
        padding: "28px 30px",
        backgroundColor: "#FFF",
        borderRadius: "24px",
        ...props.sx
     }}>
        <Typography variant="h2" sx={{
            marginBottom: "3px",
            fontWeight: 700,
            fontSize: "2.2rem",
            letterSpacing: "-1%"
        }}>{headerText}</Typography>
        {subtitleText ? <Typography variant="body1" sx={{
            marginBottom: "5px",
            fontSize: "1.6rem",
            color: "grey.900",
            letterSpacing: "-1%"
        }}>{subtitleText}</Typography> : null}
        {children}
    </Box>
}