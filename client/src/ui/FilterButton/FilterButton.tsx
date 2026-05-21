import { Button, type ButtonProps } from "@mui/material";
import FilterIcon from "@icons/filter.svg?react"

export const FilterButton = (props: ButtonProps) => {
    return <Button {...props} sx={{
        padding: "15px",
        backgroundColor: "#fff"
    }}>
        <FilterIcon />
    </Button>
}