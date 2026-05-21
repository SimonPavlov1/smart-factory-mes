import { Box, type BoxProps } from "@mui/material";

type UserAvatarProps = BoxProps & {
    size?: "xs" | "sm" | "md" | "lg"
}

const SIZES = {
    xs: {
        width: "18px",
        height: "18px",
        fontSize: "1rem"
    },
    sm: {
        width: "30px",
        height: "30px",
        fontSize: "1.4rem"
    },
    md: {
        width: "57px",
        height: "57px",
        fontSize: "2.66rem"
    },
    lg: {
        width: "100px",
        height: "100px",
        fontSize: "3.2rem"
    }
}

export const UserAvatar = ({ children, size = "md", ...props }: UserAvatarProps) => {
    const isChildrenString = typeof children === "string";

    return <Box sx={{
        display: "flex",
        justifyContent: "center",
        alignItems: "center",

        ...SIZES[size],

        borderRadius: "50%",
        fontWeight: 700,
        backgroundColor: isChildrenString ? "primary.main" : "",
        color: isChildrenString ? "#fff" : "",
    }}
        {...props}>
        {children}
    </Box>
}