import { AddButton } from "@ui/AddButton/AddButton";
import { Header } from "@components/Header/Header";
import { DatePicker } from '@ui/DatePicker/DatePicker';
import { FormControl, FormGroup, Input, InputLabel, Select, MenuItem, Box, Button, Typography, List } from "@mui/material";
import { useEffect, useState } from "react";
import { FilterButton } from "@ui/FilterButton/FilterButton";
import { Modal } from "@components/Modal/Modal";
import { Counter } from "@components/Counter/Counter";
import { RequestItem } from "./ui/RequestItem/RequestItem";
import { fetchWrapper } from "@apis/webApi";
import type { RequestProps } from "@models/request.model";


export const RequestsPage = () => {
    const [isOpen, setIsOpen] = useState(false);
    const [productCount, setProductCount] = useState(0);
    const [priority, setPriority] = useState(0);
    const [requests, setRequests] = useState([]);

    useEffect(() => {
        fetchWrapper("/api/requests")
        .then((response) => {
            setRequests(response);
        }).catch(() => {

        });
    }, [])

    return <Box>
        <Header>
            <Box className="header-row">
                <Typography variant="h1">Все заявки</Typography>
                <DatePicker sx={{ flex: 1, maxWidth: "210px" }} />
            </Box>
            <Box className="header-row" sx={{ marginTop: "28px" }}>
                <AddButton onClick={() => setIsOpen(!isOpen)}>Создать заявку</AddButton>
                <FilterButton />
            </Box>
        </Header>

        <Box component="main">
            <List>
                {requests.map((item: RequestProps) => <RequestItem item={item} key={item.id} />)}
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
                        Наименование
                    </InputLabel>
                    <Input placeholder="Укажите наименование изделия" disableUnderline />
                </FormControl>

                <FormControl>
                    <InputLabel shrink>
                        Децимальный номер
                    </InputLabel>
                    <Input placeholder="Укажите Децимальный номер" disableUnderline />
                </FormControl>

                <FormControl>
                    <InputLabel shrink>
                        Заказчик
                    </InputLabel>
                    <Input placeholder="Укажите организацию заказчика" disableUnderline />
                </FormControl>

                <FormGroup sx={{
                    flexDirection: "column",
                    gap: "19px",
                    
                    "@media (min-width: 772px)": {
                        flexDirection: "row",
                        gap: "227px",
                    },
                }}>
                    <FormControl sx={{ maxWidth: "230px", width: "100%" }}>
                        <InputLabel shrink sx={{ marginBottom: "6px" }}>
                            Укажите количество изделий
                        </InputLabel>
                        <Counter
                            value={productCount}
                            placeholder="Кол-во"
                            incrementFunc={() => setProductCount(productCount + 1)}
                            decrementFunc={() => setProductCount(productCount - 1)} />
                    </FormControl>
                    <FormControl sx={{ maxWidth: "230px", width: "100%" }}>
                        <InputLabel shrink sx={{ marginBottom: "6px" }}>
                            Дата поставки
                        </InputLabel>
                        <DatePicker iconSide="right" variantColor="white" size="lg" />
                    </FormControl>
                </FormGroup>

                <FormControl sx={{ marginTop: "25px" }}>
                    <InputLabel shrink sx={{ marginBottom: "6px" }}>
                        Приоритет
                    </InputLabel>
                    <Select name="priorities" id="priorities-select" value={priority} onChange={(event) => setPriority(event.target.value)}>
                        <MenuItem value={0}>Низкий</MenuItem>
                        <MenuItem value={1}>Средний</MenuItem>
                        <MenuItem value={2}>Высокий</MenuItem>
                    </Select>
                </FormControl>

                <Button sx={{
                    marginTop: "23.5px",
                    padding: "12px 20px",
                    width: "fit-content",
                    fontSize: "15px",
                    fontWeight: 700
                }}>
                    Создать заказ
                </Button>
            </Box>
        </Modal>
    </Box>
}