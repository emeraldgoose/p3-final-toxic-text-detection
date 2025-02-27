"""
    Meta Pseudo Labeling이 구현된 코드입니다.
    Reference: https://github.com/kekmodel/MPL-pytorch
"""
import os
import gc
import math
import random
import argparse
import pandas as pd
import numpy as np
from torch.cuda import default_stream
from sklearn.metrics import f1_score

import torch
from torch.optim.lr_scheduler import LambdaLR
import torch.nn.functional as F
from torch.utils.data.dataloader import DataLoader
from transformers import ElectraForSequenceClassification
from tokenizers import BertWordPieceTokenizer

import modeling
from utils import Config, set_seed, GOOGLE_APPLICATION_CREDENTIAL, MLFLOW_TRACKING_URI
from data import load_dataset, punctuation, punctuation2, tokenized_dataset
from tqdm import trange, tqdm

import mlflow
os.environ['GOOGLE_APPLICATION_CREDENTIALS']=GOOGLE_APPLICATION_CREDENTIAL
os.environ['MLFLOW_TRACKING_URI']=MLFLOW_TRACKING_URI

set_seed(42)

def seed_init_fn(x):
   seed = 42 + x
   np.random.seed(seed)
   random.seed(seed)
   torch.manual_seed(seed)
   return

def get_cosine_schedule_with_warmup(optimizer,
                                    num_warmup_steps,
                                    num_training_steps,
                                    num_wait_steps=0,
                                    num_cycles=0.5,
                                    last_epoch=-1):
    def lr_lambda(current_step):
        if current_step < num_wait_steps:
            return 0.0

        if current_step < num_warmup_steps + num_wait_steps:
            return float(current_step) / float(max(1, num_warmup_steps + num_wait_steps))

        progress = float(current_step - num_warmup_steps - num_wait_steps) / \
            float(max(1, num_training_steps - num_warmup_steps - num_wait_steps))
        return max(0.0, 0.5 * (1.0 + math.cos(math.pi * float(num_cycles) * 2.0 * progress)))

    return LambdaLR(optimizer, lr_lambda, last_epoch)


def train(args, tokenizer, device) -> None:
    config = Config(
        dropout1=args.dropout1,
        dropout2=args.dropout2,
        label_smoothing=args.label_smoothing,
        epochs=args.epochs,
        embedding_dim=args.embedding_dim,
        hidden_size=args.hidden_size)
    
    # Print Hyperparameters
    print(f'config : {config.__dict__}')
    mlflow.log_params(config.__dict__)

    # Train Dataset
    df = pd.read_csv('labeled.csv')
    p_df = pd.read_csv('twitch.csv')
    eval_df = pd.read_csv('test2.csv')

    # pseudo labeling할 데이터 중 2.8만개를 샘플로 사용합니다.
    true_label = p_df[(p_df['none']<p_df['curse'])==True]
    false_label = p_df.drop(true_label.index, axis=0).reset_index().drop(['index'], axis=1)
    false_label = false_label.sample(frac=args.unlabeled_sample_frac, random_state=args.seed)
    true_label = true_label.sample(frac=0.25)
    true_label = true_label.append(false_label)
    true_label = true_label.sample(frac=1, random_state=42).reset_index().drop(['index'], axis=1)
    p_df = true_label

    # weak augmentation
    p_df = punctuation(p_df)
    
    # strong augmentation
    a_df = punctuation2(p_df['text'])

    labels = list(df['label'])
    eval_labels = list(eval_df['label'])
    print(f'Test labels 0 : {eval_labels.count(0)}, 1 : {eval_labels.count(1)}')

    df = tokenized_dataset(tokenizer, df)
    p_df = tokenized_dataset(tokenizer, p_df)
    a_df = tokenized_dataset(tokenizer, a_df)
    eval_df = tokenized_dataset(tokenizer, eval_df)
    
    dataset = load_dataset(df, labels)
    p_dataset = load_dataset(p_df)
    a_dataset = load_dataset(a_df)
    eval_dataset = load_dataset(eval_df, eval_labels)

    batch_size = 32
    dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=True, worker_init_fn=seed_init_fn)
    p_dataloader = DataLoader(p_dataset, batch_size=batch_size, shuffle=True, worker_init_fn=seed_init_fn)
    a_dataloader = DataLoader(a_dataset, batch_size=batch_size, shuffle=True, worker_init_fn=seed_init_fn)
    eval_dataloader = DataLoader(eval_dataset, batch_size=batch_size, shuffle=False)
    

    # Load teacher model(pretrained), studentmodel
    teacher = ElectraForSequenceClassification.from_pretrained('jiho0304/curseELECTRA')
    
    vocab_size = args.vocab_size    
    print(f'vocab size = {vocab_size}')
    student = modeling.Model(
        vocab_size=vocab_size, 
        embedding_dim=config.embedding_dim, 
        hidden_size=config.hidden_size, 
        num_class=2,
        dropout1=config.dropout1,
        dropout2=config.dropout2)
    student.to(device)
    teacher.to(device)
    
    # Set teacher, student's optimizer
    criterion = torch.nn.CrossEntropyLoss(label_smoothing=config.label_smoothing)
    optimizer_s = torch.optim.SGD(student.parameters(), lr=args.teacher_learning_rate)
    optimizer_t = torch.optim.SGD(teacher.parameters(), lr=args.student_learning_rate)
    scaler_s = torch.cuda.amp.GradScaler()
    scaler_t = torch.cuda.amp.GradScaler()
    scheduler_t = get_cosine_schedule_with_warmup(
        optimizer=optimizer_t, num_warmup_steps=0, num_training_steps=len(p_dataloader))
    scheduler_s = get_cosine_schedule_with_warmup(
        optimizer=optimizer_s, num_warmup_steps=0, num_training_steps=len(p_dataloader))


    # Meta Pseudo Labeling
    print('------Start Training------')

    torch.cuda.empty_cache()
    gc.collect()

    best_f1 = 0
    prev_f1, patient = -1, 0
    for step in trange(len(p_dataloader) * args.epochs):
        teacher.train()
        student.train()

        labeled = next(iter(dataloader))
        unlabeled = next(iter(p_dataloader))
        a_labeled = next(iter(a_dataloader))

        # labeled data
        l_input = labeled['input_ids']
        l_attention_mask = labeled['attention_mask']
        targets = labeled['label'].to(device)

        # reference의 strong augmentation을 augmentation한 unlabeled dataset으로 가정
        a_input = a_labeled['input_ids']
        a_attention_mask = a_labeled['attention_mask']

        # reference의 weak augmentation을 unlabeled dataset으로 가정
        u_input = unlabeled['input_ids']
        u_attention_mask = unlabeled['attention_mask']
        
        with torch.cuda.amp.autocast():
            # teacher model에 먹일 input 구성 (labeled, augmention, unlabeled)
            t_input_ids = torch.cat((l_input, a_input, u_input)).to(device)
            t_attention_mask = torch.cat((l_attention_mask, a_attention_mask, u_attention_mask)).to(device)

            t_logits = teacher(input_ids=t_input_ids, attention_mask=t_attention_mask)['logits']

            t_logits_l = t_logits[:batch_size]
            t_logits_a, t_logits_u = t_logits[batch_size:].chunk(2)
        
            # teacher모델의 labeled data에 대한 loss
            t_loss_l = criterion(t_logits_l, targets)

            # augmentation을 통한 data의 label과 unlabeled data의 로스의 비교(unlabeled data로부터 증강되었기 때문)
            soft_pseudo_label = torch.softmax(t_logits_u.detach()/args.temperature, dim=-1)
            max_probs, hard_pseudo_label = torch.max(soft_pseudo_label, dim=-1)
            mask = max_probs.ge(args.threshold).float()
            t_loss_u = torch.mean( # KL.Div loss
                -(soft_pseudo_label * torch.log_softmax(t_logits_a, dim=-1)).sum(dim=-1) * mask
            )
            weight_u = args.uda_lambda * min(1., (step+1)/args.uda_step) # lambda-u, uda_step
            t_loss_uda = t_loss_l + weight_u * t_loss_u

            # student model에 먹일 input 구성 (labeled, augmention)
            s_input_ids = torch.cat((l_input, a_input)).to(device)

            s_logits = student(s_input_ids)
            s_logits = F.sigmoid(s_logits)
            s_logits_l, s_logits_a = s_logits[:batch_size], s_logits[batch_size:]

            # 업데이트 되지 않은 student 모델의 labeled data에 대한 로스값(labeled data에 대한 validation)
            s_loss_l_old = F.cross_entropy(s_logits_l.detach(), targets)
            
            # augmented data에 대해서 student가 학습
            s_loss = criterion(s_logits_a, hard_pseudo_label)

        scaler_s.scale(s_loss).backward()
        scaler_s.step(optimizer_s)
        scaler_s.update()
        scheduler_s.step()

        with torch.cuda.amp.autocast():
            # 업데이트 된 student 모델의 labeled data에 대한 로스
            with torch.no_grad():
                s_logits_l = student(l_input.to(device))
                s_logits_l = F.sigmoid(s_logits_l)
                s_loss_l_new = F.cross_entropy(s_logits_l.detach(), targets)
            
            # teacher coefficient : https://github.com/kekmodel/MPL-pytorch/issues/6
            dot_product = s_loss_l_old - s_loss_l_new

            # compute the teacher's gradient from student's feedback
            _, hard_pseudo_label = torch.max(t_logits_a.detach(), dim=-1)
            t_loss_mpl = dot_product * F.cross_entropy(t_logits_a, hard_pseudo_label)
            
            t_loss = t_loss_uda + t_loss_mpl # t_loss_uda = t_loss_l + t_loss_unlabeled

        scaler_t.scale(t_loss).backward()
        scaler_t.step(optimizer_t)
        scaler_t.update()
        scheduler_t.step()

        teacher.zero_grad()
        student.zero_grad()
    
        # step마다 Evalution 진행
        if step > 0 and step % 10 == 0:
            student.eval()
            correct, loss = 0, 0
            zero, one = 0, 0
            prediction = []
            with torch.no_grad():
                for _, batch in tqdm(enumerate(eval_dataloader)):
                    data = batch['input_ids'].cuda()
                    labels = batch['label']
                    output = student(data)
                    predicted = torch.max(output,1)[1]
                    
                    prediction += predicted.tolist()
                    zero += predicted.tolist().count(0)
                    one += predicted.tolist().count(1)
                    
                    correct += (predicted==labels.cuda()).sum()
                    loss += F.cross_entropy(output, labels.cuda()).item()

            eval_f1 = f1_score(eval_labels, prediction, average='macro')
            print(f'Epoch: {step+1} | Train Loss : {loss/len(eval_dataloader):.5f} | Test Acc : {correct/len(eval_dataset):.5f} | Zero : {zero} | One : {one} | F1 : {eval_f1:.5f}')

            if eval_f1 > best_f1:
                # 가장 좋을 때의 모델을 저장합니다.
                torch.save(student.state_dict(), f'./save/meta_pseudo/result_temp.pt')
                mlflow.pyfunc.log_model(student, 'model', registered_model_name='toxicity_text')
                best_f1 = eval_f1
        
            if prev_f1 == eval_f1:
                patient += 1
                if patient == args.patient:
                    break
            else:
                patient = 0
            prev_f1 = eval_f1

    print(f'best f1 = {best_f1}')


def finetune(tokenizer, device):
    """
        MPL이 적용된 student 모델을 다시 labeled data로 Fine tuning합니다.
    """
    # Load datasets
    df = pd.read_csv('labeled.csv')
    eval_df = pd.read_csv('test2.csv')
    
    labels = list(df['label'])
    eval_labels = list(eval_df['label'])

    df = punctuation(df)
    
    df = tokenized_dataset(tokenizer, df)
    eval_df = tokenized_dataset(tokenizer, eval_df)
    
    dataset = load_dataset(df, labels)
    eval_dataset = load_dataset(eval_df, eval_labels)
    
    batch_size = args.batch_size
    dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=True)
    eval_dataloader = DataLoader(eval_dataset, batch_size=batch_size, shuffle=False)
    
    # Load model
    epochs = args.finetune_epochs
    model = modeling.Model(
        vocab_size=args.vocab_size,
        embedding_dim=args.embedding_dim,
        hidden_size=args.hidden_size,
        num_class=args.num_classes,
        dropout1=args.dropout1,
        dropout2=args.dropout2
    )
    model.load_state_dict(torch.load('./save/meta_pseudo/result_temp.pt'))
    model.to(device)

    # Set criterion, optimizer, scheduler
    criterion = torch.nn.CrossEntropyLoss(label_smoothing=args.label_smoothing)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.finetune_learning_rate)
    scheduler = torch.optim.lr_scheduler.OneCycleLR(
        optimizer=optimizer,
        epochs = epochs,
        max_lr=args.finetune_max_lr,
        steps_per_epoch=len(dataloader),
        pct_start=args.finetune_pct_start,
    )
    
    best_f1 = 0
    for epoch in range(epochs):
        running_loss = 0
        model.train()
        for i, labeled in enumerate(dataloader):
            input = labeled['input_ids'].to(device)
            label = labeled['label'].to(device)

            output = model(input)
            loss = criterion(output, label)
            running_loss += loss.item()

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            scheduler.step()
        
        with torch.no_grad():
            # Evalutaion
            model.eval()
            correct = 0
            prediction = []
            for j, batch in enumerate(eval_dataloader):
                input = batch['input_ids'].cuda()
                label = batch['label'].cuda()
                
                output = model(input)
                preds = output.argmax(-1)
                prediction += preds.tolist()
                correct += (preds==label).sum().item()
            
            eval_acc = correct/len(eval_dataset)
            f1 = f1_score(eval_labels, prediction, average='macro')

        print(f'Epoch: {epoch+1} | Train Loss : {running_loss/len(dataloader):.5f} | Acc : {eval_acc:.5f} | F1 : {f1:.3f}')
        mlflow.log_metric('train loss', running_loss/len(dataloader))
        mlflow.log_metric('eval acc', eval_acc)
        mlflow.log_metric('eval f1', f1)

        if f1 > best_f1:
            torch.save(model.state_dict(), f'./save/temp/result_{f1:.3f}.pt')
            best_model = model.state_dict()
            best_f1 = f1
    
    # mlflow tracking model
    mlflow.pytorch.log_model(best_model, 'model', registered_model_name="ToxicityText")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Meta Pseudo Labeling(Reference : https://github.com/kekmodel/MPL-pytorch)')
    parser.add_argument('--dropout1', default=0.3, type=float, help='dropout embedding layer, linear layer')
    parser.add_argument('--dropout2', default=0.4, type=float, help='dropout conv layer')
    parser.add_argument('--teacher_learning_rate', default=1e-7, type=float, help='mpl teacher learning rate')
    parser.add_argument('--student_learning_rate', default=1e-7, type=float, help='mpl student learning rate')
    parser.add_argument('--label_smoothing', default=0, type=float, help='mpl traning label smoothing')
    parser.add_argument('--embedding_dim', default=100, type=int, help='model embedding dimension')
    parser.add_argument('--hidden_size', default=128, type=int, help='model hidden size')
    parser.add_argument('--num_classes', default=2, type=int, help='number of classification')

    parser.add_argument('--epochs', default=1, type=int, help='mpl trainig epochs')
    parser.add_argument('--seed', default=42, type=int, help='random seed')
    parser.add_argument('--vocab_size', default=30000, type=int, help='tokenizer vocab size')
    parser.add_argument('--batch_size', default=32, type=int, help='mpl training batch size')
    parser.add_argument('--unlabeled_sample_frac', default=0.025, type=float, help='unlabeled dataset sample ratio')
    parser.add_argument('--temperature', default=0.9, type=float, help='pseudo label temperature')
    parser.add_argument('--uda_lambda', default=1.0, type=float, help='pseudo label weight lambda')
    parser.add_argument('--uda_step', default=1.0, type=float, help='pseudo label uda step')
    parser.add_argument('--threshold', default=0.6, type=float, help='pseudo label threshold')
    parser.add_argument('--patient', default=20, type=int, help='mpl early stopping patient')

    parser.add_argument('--finetune_learning_rate', defulat=0.001, type=float, help='finetuning learning rate')
    parser.add_argument('--finetune_epochs', defulat=10, type=int, help='finetuning epochs')
    parser.add_argument('--finetune_max_lr', defulat=0.01, type=float, help='finetuning OneCyclelr scheduler max_lr')
    parser.add_argument('--finetune_pct_start', defulat=0.1, type=float, help='finetuning OncCyclelr scheduler pct_start')

    args = parser.parse_args()

    # device
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f'device = {device}')

    # load tokenizer
    tokenizer = BertWordPieceTokenizer('./vocab.txt', lowercase=False)
    
    # MPL 수행 후 labeled data에 대해 finetuning을 시도합니다
    train(args, tokenizer, device)
    finetune(args, tokenizer, device)
