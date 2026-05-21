import { Header } from "@/components/Header/Header";
import { Box, Typography } from "@mui/material";
import { DatePicker } from "@ui/DatePicker/DatePicker";
import { BarChart } from "@components/BarChart/BarChart";
import { Panel } from "@ui/Panel/Panel";
import { ActivitiesList } from "@components/ActivitiesList/ActivitiesList";

export const PanelsPage = () => {
    const datasets = [
        {
            data: [21, 11, 4, 32, 55, 73, 21]
        }
    ];

    const labels = ["2021", "2022", "2023", "2024", "2025", "2026", "2027"];
    
    return <Box>
        <Header>
            <Box className="header-row">
                <Typography variant="h1" sx={{ fontSize: "3.6rem" }}>Панель</Typography>
                <DatePicker sx={{ flex: 1, maxWidth: "210px" }} />
            </Box>
            <Box className="header-row">
                Добро пожаловать Дмитрий!
            </Box>
        </Header>

        <Box component="main" sx={{
            display: "grid",
            gridTemplate: {
                xs: `
            "panel1 panel3"
            "panel2 panel3"
            "panel4 panel3"
            / minmax(10vw, 1.2fr) minmax(10vw, 1fr)`,
                lg: `
            "panel1 panel2 panel3"
            "panel4 panel4 panel3"
            / minmax(10vw, 1.2fr) minmax(10vw, 1.2fr) minmax(12vw, 1fr)`
            },
            gap: "10px",
            marginTop: "10px"
        }}>
            <Panel headerText="Всего заявок" subtitleText="Помесячно" sx={{ gridArea: "panel1" }}>
                <BarChart datasets={datasets} labels={labels} activeInd={3} />
            </Panel>
            <Panel headerText="Выполнено" subtitleText="Помесячно" sx={{ gridArea: "panel2" }}>
                <BarChart datasets={datasets} labels={labels} activeInd={1} />
            </Panel>
            <Panel headerText="Брак" subtitleText="Выполнено" sx={{ gridArea: "panel4" }}>
                <BarChart datasets={datasets} labels={labels} activeInd={6} />
            </Panel>
            <Panel headerText="Активность" sx={{ padding: "28px 10px 14.5px 10px", gridArea: "panel3" }}>
                <ActivitiesList />
            </Panel>
        </Box>

    </Box>
}