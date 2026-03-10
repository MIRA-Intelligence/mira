import torch
from monai.networks.nets import UNet
from monai.losses import DiceCELoss
from monai.metrics import DiceMetric
from monai.inferers import sliding_window_inference
from template_dataset import build_data_loaders

def train_monai_model(data_dir, max_epochs=50, device="cuda"):
    train_loader, val_loader = build_data_loaders(data_dir, batch_size=2)
    device = torch.device(device if torch.cuda.is_available() else "cpu")
    
    # 1. Define Model
    model = UNet(
        spatial_dims=3,
        in_channels=1,
        out_channels=2, # Binary including background
        channels=(16, 32, 64, 128, 256),
        strides=(2, 2, 2, 2),
        num_res_units=2
    ).to(device)
    
    # 2. Loss & Optimizer
    loss_function = DiceCELoss(to_onehot_y=True, softmax=True)
    optimizer = torch.optim.AdamW(model.parameters(), 1e-4)
    dice_metric = DiceMetric(include_background=False, reduction="mean")
    
    # 3. Standard Loop
    best_metric = -1
    for epoch in range(max_epochs):
        print(f"Epoch {epoch+1}/{max_epochs}")
        model.train()
        epoch_loss = 0
        
        for batch_data in train_loader:
            inputs, labels = batch_data["image"].to(device), batch_data["label"].to(device)
            optimizer.zero_grad()
            outputs = model(inputs)
            loss = loss_function(outputs, labels)
            loss.backward()
            optimizer.step()
            epoch_loss += loss.item()
            
        print(f"Train Loss: {epoch_loss/len(train_loader):.4f}")
        
        # Validation
        model.eval()
        with torch.no_grad():
            for val_data in val_loader:
                val_inputs, val_labels = val_data["image"].to(device), val_data["label"].to(device)
                
                # Inference via Sliding Window
                val_outputs = sliding_window_inference(val_inputs, (96, 96, 96), 4, model)
                val_outputs = torch.argmax(val_outputs, dim=1, keepdim=True)
                
                dice_metric(y_pred=val_outputs, y=val_labels)
                
            metric = dice_metric.aggregate().item()
            dice_metric.reset()
            print(f"Val Dice: {metric:.4f}")
            
            if metric > best_metric:
                best_metric = metric
                torch.save(model.state_dict(), "best_monai_model.pth")
                print("Saved new best model.")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--data_dir", type=str, required=True)
    args = parser.parse_args()
    train_monai_model(args.data_dir)
