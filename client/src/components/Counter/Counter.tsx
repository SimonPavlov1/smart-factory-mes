import { Box, Button, Input, type BoxProps } from "@mui/material";
import ArrowIcon from "@icons/arrow.svg?react";

type CounterProps = BoxProps & {
    value: number,
    placeholder: string,
    incrementFunc: () => void,
    decrementFunc: () => void
}

export const Counter = ({ value, placeholder, incrementFunc, decrementFunc, onChange, ...props }: CounterProps) => {
    return <Box sx={{
        display: "flex",
        alignItems: "center",
        borderRadius: "13px",
        backgroundColor: "#fff"
    }} {...props}>

        <Input value={value} placeholder={placeholder} onChange={onChange} disableUnderline sx={{
            flex: 1,
            margin: 0,
            backgroundColor: "transparent",
            border: "none",
            fontSize: "1.32rem"
        }} />

        <Box sx={{ display: "inline-flex", flexDirection: "column", gap: "2.8px" }}>
            <Button onClick={incrementFunc} sx={{ padding: "4px 4px 0 2px", backgroundColor: "transparent" }}>
                <ArrowIcon />
            </Button>
            <Button onClick={decrementFunc} sx={{ padding: "4px 4px 0 2px", transform: "rotateX(180deg)", backgroundColor: "transparent" }}>
                <ArrowIcon />
            </Button>
        </Box>
    </Box>
}