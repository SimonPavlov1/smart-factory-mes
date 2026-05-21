import ChartDataLabels from "chartjs-plugin-datalabels";
import {
    Chart as ChartJS,
    CategoryScale,
    LinearScale,
    BarElement,
    Title,
    Tooltip,
    Legend,
    type ChartDataset
} from 'chart.js';
import { Bar } from 'react-chartjs-2';

type BarChartProps = {
    datasets: ChartDataset<"bar">[],
    labels: string[],
    activeInd: number
}

ChartJS.register(
    CategoryScale,
    LinearScale,
    BarElement,
    Title,
    Tooltip,
    Legend,
    ChartDataLabels
);

export const BarChart = ({ datasets, labels, activeInd }: BarChartProps) => {
    const options = {
        responsive: true,
        plugins: {
            legend: {
                display: false
            },
            tooltip: {
                enabled: false
            }
        },
        scales: {
            y: {
                display: false
            },
            x: {
                grid: {
                    display: false
                },
                border: {
                    display: false
                }
            },
        }
    };

    const data = {
        labels,
        datasets: datasets.map((item) => {
            return {
                backgroundColor: item.data.map((_, ind) => ind === activeInd ? "#3F8CFF" : "#ECF3FF"),
                borderRadius: 14,
                borderSkipped: false,
                datalabels: {
                    formatter: (value, context) => {
                        return context.dataIndex === activeInd ? value : '';
                        },
                        anchor: 'end',
                        align: 'top',
                        color: '#333',
                        font: { weight: 'bold', size: 14 }
                },
                ...item
            }
        }),
    };

    return <Bar options={options} data={data} />
}