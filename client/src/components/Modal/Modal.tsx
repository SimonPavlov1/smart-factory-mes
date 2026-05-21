import { Button, Modal as ModalMui, Box, Typography, type ModalProps as ModalMuiProps } from "@mui/material";
import CloseIcon from "@icons/close.svg?react";

type ModalProps = ModalMuiProps & {
    setIsOpen: React.Dispatch<React.SetStateAction<boolean>>,
    headerText: string
}

export const Modal = ({ children, setIsOpen, headerText, ...props }: ModalProps) => {
    return <ModalMui component="div" {...props} sx={{ 
        display: "flex",
        justifyContent: "center",
        alignItems: "center"
     }}>
        <Box component="section" sx={{
            maxWidth: "1000px",
            flex: 1,
            padding: "14.4px 37px 17.5px",
            border: "none",
            backgroundColor: "background.default",
            borderRadius: "24px"
        }}>
            <Box component="header" sx={{ 
                display: "flex",
                justifyContent: "space-between"
             }}>
                <Typography variant="h2" sx={{
                    fontSize: "4.8rem"
                }}>{headerText}</Typography>

                <Button onClick={() => setIsOpen(false)} sx={{
                    padding: "10px",
                    width: "44px",
                    height: "44px",
                    backgroundColor: "#EBEBEB",
                }}>
                    <CloseIcon />
                </Button>
            </Box>
            {children}
        </Box>
    </ModalMui>
}