import pandas as pd
from lifelines import KaplanMeierFitter, CoxPHFitter
from lifelines.statistics import logrank_test
import matplotlib.pyplot as plt

def plot_km(df, duration_col, event_col, group_col, output_plot):
    """
    Plot Kaplan-Meier Curves comparing groups.
    """
    kmf = KaplanMeierFitter()
    plt.figure(figsize=(8,6))
    
    # Drop NaNs
    df_clean = df.dropna(subset=[duration_col, event_col, group_col])
    groups = df_clean[group_col].unique()
    
    for g in groups:
        idx = df_clean[group_col] == g
        kmf.fit(df_clean.loc[idx, duration_col], df_clean.loc[idx, event_col], label=f"{group_col}={g}")
        kmf.plot_survival_function()
        
    plt.title('Kaplan-Meier Survival Curve')
    plt.xlabel('Time')
    plt.ylabel('Survival Probability')
    plt.grid(True, alpha=0.3)
    
    plt.savefig(output_plot, dpi=300, bbox_inches='tight')
    print(f"Saved KM curve to {output_plot}")
    
    # Log-rank test for 2 groups
    if len(groups) == 2:
        idx0 = df_clean[group_col] == groups[0]
        idx1 = df_clean[group_col] == groups[1]
        
        res = logrank_test(
            df_clean.loc[idx0, duration_col], df_clean.loc[idx1, duration_col],
            df_clean.loc[idx0, event_col], df_clean.loc[idx1, event_col]
        )
        print(f"Log-rank p-value: {res.p_value:.4e}")

def run_cox(df, duration_col, event_col, covariates=None):
    """
    Fit a multivariable Cox Proportional Hazards model.
    """
    if covariates:
        df_model = df[[duration_col, event_col] + covariates].dropna()
    else:
        df_model = df.dropna()
        
    cph = CoxPHFitter()
    cph.fit(df_model, duration_col=duration_col, event_col=event_col)
    
    print("\n--- Cox Proportional Hazards Model Summary ---")
    cph.print_summary()
    print(f"Concordance Index (C-index): {cph.concordance_index_:.4f}")
    return cph

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Survival Analysis Pipeline")
    parser.add_argument('--csv', required=True, help="Input CSV data")
    parser.add_argument('--time', required=True, help="Time/Duration column name")
    parser.add_argument('--event', required=True, help="Event status (1=Occurred, 0=Censored) column name")
    parser.add_argument('--group', default=None, help="Column name to stratify KM curves")
    parser.add_argument('--plot_out', default='km_plot.png', help="Output plot path")
    args = parser.parse_args()
    
    df = pd.read_csv(args.csv)
    
    if args.group:
        plot_km(df, args.time, args.event, args.group, args.plot_out)
    else:
        print("No group specified for KM. Running baseline Cox on all variables.")
        run_cox(df, args.time, args.event)
