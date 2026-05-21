import { DatePicker as MuiDatePicker, type DatePickerProps } from "@mui/x-date-pickers";
import CalendarIcon from "@icons/calendar.svg?react";

type CustomeDatePickerProps = DatePickerProps & {
    variantColor?: "white" | "dark",
    iconSide?: "left" | "right",
    size?: "sm" | "lg"
}

const VARIANTS = {
    dark: {
        backgroundColor: "#E6EDF5",
    },
    white: {
        backgroundColor: "#FFF",
    },
    left: {
        order: -1
    },
    right: {
        order: 1
    }
}

export const DatePicker = ({ variantColor = "dark", iconSide = "left", size = "sm", sx, ...props }: CustomeDatePickerProps) => {
    return <MuiDatePicker {...props} slots={{
        openPickerIcon: CalendarIcon
    }} slotProps={{
        inputAdornment: {
            sx: {
                margin: 0,
                ...VARIANTS[iconSide]
            }
        },

        openPickerButton: {
            sx: {
                width: "20px",
                height: "20px",
                padding: 0,
            }
        }
    }} sx={{
        ...sx,
        "& .MuiPickersInputBase-root": {
            padding: size === "sm" ? "6px 16px" : "11px 17.5px",
            gap: "24px",
            borderRadius: "14px",
            border: "none",
            fontSize: "1.6rem",
            color: "#0A1629",
            ...VARIANTS[variantColor]
        },
        "& .MuiPickersSectionList-root": {
            width: "fit-content",
            padding: 0,
            justifyContent: iconSide === "left" ? "flex-end" : "flex-start"
        },
        "& .MuiPickersOutlinedInput-notchedOutline": {
            border: 0
        },
    }} />
}