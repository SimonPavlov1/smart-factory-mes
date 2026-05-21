import { Button, type ButtonProps } from "@mui/material";
import AddIcon from "@icons/add.svg?react";

export const AddButton = ({ children, ...props }: ButtonProps) => {
    return <Button {...props} sx={{
        display: "flex",
        gap: "13px",
        padding: "15px 18px",
        fontSize: "min(2.3vw, 1.8rem)",
        fontWeight: 700
    }}>
        <AddIcon />
        {children}
    </Button>
}