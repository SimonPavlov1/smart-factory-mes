import './App.css';
import { RouterProvider } from 'react-router/dom';
import router from './router';
import { createTheme, ThemeProvider } from '@mui/material/styles';
import { CssBaseline } from '@mui/material';
import { LocalizationProvider } from '@mui/x-date-pickers';
import { AdapterDayjs } from '@mui/x-date-pickers/AdapterDayjs';
import 'dayjs/locale/ru';

const THEME = createTheme({
    palette: {
      primary: {
        main: "#3F8CFF",
      },
      secondary: {
        main: "#3F8CD0"
      },
      error: {
        500: "#FF0000"
      },
      grey: {
        500: "#91929E",
        900: "#7D8592"
      },
      background: {
        default: "#F3F8FC",
        paper: "#FFFFFF"
      }
    },

    typography: {
      fontFamily: "Nunito sans, sans-serif",

      h1: {
        fontSize: "min(4vw, 3rem)",
        fontWeight: 700
      },

      h2: {
        fontWeight: 700
      },

      h3: {
        fontWeight: 700,
        fontSize: "1.578rem"
      }
    },

    components: {
      MuiFormControl: {
        styleOverrides: {
          "root": {
            marginTop: "19px",

            ".MuiFormGroup-root &": {
              margin: 0
            }
          }
        }
      },

      MuiFormGroup: {
        styleOverrides: {
          root: {
            marginTop: "19px"
          }
        }
      },

      MuiInputBase: {
        styleOverrides: {
          root: {
            marginTop: "10px",
            border: "none",
          },
          input: {
            "&:-webkit-autofill": {
              WebkitBoxShadow: "0 0 0 100px #FFF inset"
            },
          }
        }
      },

      MuiInput: {
        styleOverrides: {
          root: {
            padding: "11px 21px 11px 17px",
            borderRadius: "14px",
            border: "1px solid #D8E0F0",
            fontSize: "1.32rem",
            backgroundColor: "#FFFFFF",

            ":hover": {
              border: "1px solid #000"
            },
          },
          formControl: {
            'label + &, .MuiInputLabel-root + &': {
              marginTop: "6px",
            },
          },
          
        },
      },

      MuiInputLabel: {
        styleOverrides: {
          root: {
            transform: "none",
            position: "static",
            fontSize: "1.32rem",
            fontWeight: 700,
            color: "#7D8592"
          }
        }
      },

      MuiSelect: {
        styleOverrides: {
          root: {
            margin: 0,
            padding: 0,
            borderRadius: "14px",
            border: "1px solid #D8E0F0",
            fontSize: "1.32rem",
            backgroundColor: "#FFFFFF",
          }
        }
      },

      MuiPaper: {
        styleOverrides: {
          root: {
            "& *": {
              fontSize: "1.32rem",
            }
          }
        }
      },

      MuiButton: {
        styleOverrides: {
          root: ({ theme }) => ({
            minWidth: "min-content",
            borderRadius: "14px",
            backgroundColor: theme.palette.primary.main,
            color: "#FFF",
            textTransform: "none"
          })
        }
      },

      MuiLink: {
        styleOverrides: {
          root: {
            textDecoration: "none"
          }
        }
      }
    }
  });

function App() {
  return <LocalizationProvider dateAdapter={AdapterDayjs} adapterLocale='ru'>
    <ThemeProvider theme={THEME}>
      <CssBaseline />
      <RouterProvider router={router} />
  </ThemeProvider>
  </LocalizationProvider>
}

export default App
