import { Header } from "@components/Header/Header";
import { AddButton } from "@ui/AddButton/AddButton";
import { FilterButton } from "@ui/FilterButton/FilterButton";
import { PersonItem } from "./ui/PersonItem/PersonItem";
import { Box, Button, FormControl, Input, InputLabel, List } from "@mui/material"
import { useEffect, useState } from "react";
import { fetchWrapper } from "@apis/webApi";
import { Modal } from "@components/Modal/Modal";
import type { PersonProps } from "@models/person.model";

export const StaffPage = () => {
    const [staff, setStaff] = useState<PersonProps[]>([]);
    const [isOpen, setIsOpen] = useState(false);

    useEffect(() => {
        fetchWrapper("/api/staff").then((staff: PersonProps[]) => {
            setStaff(staff);
        })
    }, [])

    return <Box>
        <Header>
            <Box className="header-row" sx={{ marginTop: "13px" }}>
                <AddButton onClick={() => setIsOpen(true)}>
                    Добавить персонал
                </AddButton>
                <FilterButton />
            </Box>
        </Header>
        <Box component="main" sx={{ marginTop: "30px" }}>
            <List>
                {staff.map((person: PersonProps) => <PersonItem person={person} key={person.email} />)}
            </List>
        </Box>

        <Modal open={isOpen} setIsOpen={setIsOpen} headerText="Создание заявки">
            <Box component="form" sx={{
                display: "flex",
                flexDirection: "column",
                maxWidth: "735px",
                marginLeft: "13px"
            }}>
                <FormControl sx={{ marginTop: "9px" }}>
                    <InputLabel shrink>
                        ФИО
                    </InputLabel>
                    <Input placeholder="Укажите наименование изделия" disableUnderline />
                </FormControl>

                <FormControl>
                    <InputLabel shrink>
                        Должность
                    </InputLabel>
                    <Input placeholder="Укажите Децимальный номер" disableUnderline />
                </FormControl>

                <FormControl>
                    <InputLabel shrink>
                        Телефон
                    </InputLabel>
                    <Input placeholder="Укажите организацию заказчика" disableUnderline />
                </FormControl>

                <FormControl>
                    <InputLabel shrink>
                        Email
                    </InputLabel>
                    <Input placeholder="Укажите организацию заказчика" type="email" disableUnderline />
                </FormControl>

                <Button sx={{
                    marginTop: "23.5px",
                    padding: "12px 20px",
                    width: "fit-content",
                    fontSize: "15px",
                    fontWeight: 700
                }}>
                    Добавить сотрудника
                </Button>
            </Box>
        </Modal>
    </Box>
}