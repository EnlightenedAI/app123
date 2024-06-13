from transformers import MT5Model,MT5ForConditionalGeneration, MT5TokenizerFast, MT5Config
from torch.utils.data import DataLoader, Dataset
import torch
import time
import pdb
import numpy as np
from torch.cuda.amp import autocast, GradScaler
from train_eval import train,test
from importlib import import_module
import argparse
from tqdm import tqdm
from utils import build_dataset, get_time_dif
from utils_multi import build_dataset_multi, get_time_dif
from torch.utils.data import TensorDataset, DataLoader
from torchsampler import ImbalancedDatasetSampler
from torch.nn import DataParallel
import torch.distributed as dist
from sklearn.metrics import recall_score,f1_score,precision_score,roc_auc_score
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.data import DataLoader, DistributedSampler
from pytorch_pretrained.optimization import BertAdam
from eval import rouge_scorces
# from torch.optim import BertAdam
import copy
import os
import json
classify =False
rewrite=True
nccl=False
test_step= True
train_step=True
# os.environ['OMP_NUM_THREADS'] = '1'
os.environ['CUDA_VISIBLE_DEVICES'] = '0'
os.environ['PYTORCH_CUDA_ALLOC_CONF'] = 'max_split_size_mb=64,initial_growth_trigger_mb=512'
# 设置主节点的地址和端口
# local_rank = int(os.environ['LOCAL_RANK'])
# # 设置当前进程应该使用的GPU
# torch.cuda.set_device(local_rank)
# os.environ['MASTER_ADDR'] = '192.168.1.1'  # 示例IP地址
# os.environ['MASTER_PORT'] = '12345'        # 示例端口号

# # 设置总进程数和当前进程的rank
# os.environ['WORLD_SIZE'] = '3'  # 假设有3个进程
# os.environ['RANK'] = '0'        # 假设当前进程的rank是0
from Data_loading import dataload
from loss.focallooss import Focal_Loss,Focal_Loss_multi
# 检查是否安装了必要的库
try:
    from torch.utils.data.distributed import DistributedSampler
except ImportError:
    raise ImportError("DistributedSampler required for distributed training.")
# 1. 初始化进程组
if nccl:
    dist.init_process_group(
            backend='nccl',  # 对于GPU训练，通常使用'nccl'
        )
binary_loss=Focal_Loss(alpha=0.4, gamma=2) 
multi_loss=Focal_Loss_multi(alpha=0.4, gamma=2) 
# loss_fct = torch.nn.CrossEntropyLoss(reduction='mean')
loss_fct=torch.nn.CrossEntropyLoss(ignore_index=-100)
    # def generate(self,config):
    #     generate=self.generate(config)  
    #     return
from transformers import EncoderDecoderModel, BertTokenizer, GPT2Tokenizer
from transformers import ElectraTokenizer, ElectraModel,AutoTokenizer,GPT2Config

# 加载BERT和GPT的Tokenizer
encoder_tokenizer = AutoTokenizer.from_pretrained('xlnet/xlnet-base-cased')
decoder_tokenizer = GPT2Tokenizer.from_pretrained('openai-community/gpt2')
decoder_tokenizer.pad_token =decoder_tokenizer.eos_token
model = EncoderDecoderModel.from_encoder_decoder_pretrained('xlnet/xlnet-base-cased', 'openai-community/gpt2')
# bert2gpt_model = EncoderDecoderModel.from_encoder_decoder_pretrained("google-bert/bert-base-cased", "openai-community/gpt2")
model.config.decoder_start_token_id = decoder_tokenizer.bos_token_id
model.config.pad_token_id = decoder_tokenizer.pad_token_id

# print(decoder_tokenizer.pad_token_id)
# pdb.set_trace()
# model_name = "google/mt5-small"
# tokenizer = MT5TokenizerFast.from_pretrained(model_name)
# model = MT5ForConditionalGeneration.from_pretrained(model_name)
config = AutoTokenizer.from_pretrained('xlnet/xlnet-base-cased')
config.hidden_size=768
config.pad_size=150
dconfig=GPT2Config.from_pretrained('openai-community/gpt2')
config.decoder_vocab_size=dconfig.vocab_size
config.tokenizer=encoder_tokenizer
class CustomMT5Model(torch.nn.Module):
    def __init__(self,config,model):
        super().__init__()
        self.important = torch.nn.Linear(config.hidden_size,2)
        self.risk = torch.nn.Linear(config.hidden_size,2)
        self.sensitive = torch.nn.Linear(config.hidden_size,2)
        self.multi = torch.nn.Linear(config.hidden_size,9)
        self.model=model
        self.config=config
    def forward(self, input_ids=None, attention_mask=None, labels=None, decoder_input_ids=None, decoder_attention_mask=None,encoder_outputs=None,task='multi'):
        # 编码器部分
        if not encoder_outputs:
            encoder_outputs = self.model.encoder(input_ids, attention_mask=attention_mask)
        # print(encoder_outputs)
        if task=='important':
            # 二分类任务，使用编码器的输出
            # print(labels)
            # print(encoder_outputs.last_hidden_state.shape)
            classification_output = self.important(encoder_outputs.last_hidden_state[:,0,:])
            # print(classification_output.shape)
            if not labels is None:
                loss = binary_loss(classification_output, labels)
            # loss.backward()
                return loss,classification_output
            else:
                return classification_output
        elif task=='risk':
            # 二分类任务，使用编码器的输出
            # print(labels)
            # print(encoder_outputs.last_hidden_state.shape)
            classification_output = self.risk(encoder_outputs.last_hidden_state[:,0,:])
            # print(classification_output.shape)
            if not labels is None:
                loss = binary_loss(classification_output, labels)
            # loss.backward()
                return loss,classification_output
            else:
                return classification_output  
        elif task=='sensitive':
            # 二分类任务，使用编码器的输出
            # print(labels)
            # print(encoder_outputs.last_hidden_state.shape)
            classification_output = self.sensitive(encoder_outputs.last_hidden_state[:,0,:])
            # print(classification_output.shape)
            if not labels is None:
                loss = binary_loss(classification_output, labels)
            # loss.backward()
                return loss,classification_output
            else:
                return classification_output  
        elif task=='multi':
            # 多分类任务，使用编码器的输出
            # print(encoder_outputs.last_hidden_state.shape)
            classification_output = self.multi(encoder_outputs.last_hidden_state[:,0,:])
            # print(classification_output.shape)
        
            
            if not labels is None:
                loss = multi_loss(classification_output, labels)
                return loss,classification_output
            else:
                return classification_output

        elif task=='rewrite':
            # 生成任务，使用解码器
            # 确保 decoder_input_ids 是正确传递的
            decoder_outputs = self.model.decoder(
                input_ids=decoder_input_ids,
                attention_mask=decoder_attention_mask,
                # past_key_values=encoder_outputs.past_key_values,
                encoder_hidden_states=encoder_outputs.last_hidden_state,
                encoder_attention_mask=attention_mask
            )
            sequence_output = decoder_outputs[0]
            prediction_scores=decoder_outputs.logits
            # prediction_scores = self.model.lm_head(sequence_output)
            # prediction_scores=prediction_scores.view(-1, self.config.vocab_size)
            # print('预测：',prediction_scores.device,prediction_scores.shape)
            loss = None
            # print(labels)
            # pdb.set_trace()
            if labels is not None:
                loss = loss_fct(prediction_scores.view(-1, config.decoder_vocab_size), labels.view(-1))
                # print('loss：',loss.device,loss)
                
            return  loss
        elif task=='generate':
            # with torch.no_grad():
            output=self.model.generate(
                input_ids=input_ids,
                attention_mask=attention_mask,
                max_length=250,
                early_stopping=True,
                num_beams=10,
                num_return_sequences=1,
                no_repeat_ngram_size=2,
            )
            # output = self.model.decoder.generate(input_ids, encoder_outputs=encoder_outputs, max_length=50, num_beams=4, early_stopping=True)
                # print('一个一个又一个')
            return output
# config.model=modelx
model = CustomMT5Model(config,model)
# 2. 准备改写数据集
# class TranslationDataset(Dataset):
        # def __init__(self, src_texts, trg_texts, tokenizer):
        #     self.src_texts = src_texts
        #     self.trg_texts = trg_texts
        #     self.tokenizer = tokenizer

        # def __len__(self):
        #     return len(self.src_texts)

        # def __getitem__(self, idx):
        #     src_text = self.src_texts[idx]
        #     trg_text = self.trg_texts[idx]
        #     inputs = tokenizer(src_text, return_tensors="pt", padding=True, truncation=True)
        #     targets = tokenizer(trg_text, return_tensors="pt", padding=True, truncation=True)
            # return {"input_ids": inputs["input_ids"].squeeze(), "attention_mask": inputs["attention_mask"].squeeze(), "labels": targets["input_ids"].squeeze()}
if rewrite:
    # 示例数据
    # batch_size=8
    max_len_inp=250
    max_len_out=150
    max_epochs=100
    train_is_ture=True
    testdata_is_ture=True
    lr=3e-4
    data_path=f'TCSI_pp/preprocessing/results_rewrite/'
    # savepath=f"./result/mt5_ddp_epoch{max_epochs}.ckpt" #save model
    save_rewrite_path=f'./result/xlnet2gpt_rewrite_ddp_epoch{max_epochs}.json' #save rewrite result
    true_false_adjective_tuples_train,true_false_adjective_tuples_validation,true_false_adjective_tuples_test=dataload(data_path)


    class FalseGenerationDataset(Dataset):
        def __init__(self, encoder_tokenizer,decoder_tokenizer, tf_list, max_len_inp=150, max_len_out=150):
            self.true_false_adjective_tuples = tf_list
            self.max_len_input = max_len_inp
            self.max_len_output = max_len_out
            self.encoder_tokenizer = encoder_tokenizer #token-model
            self.decoder_tokenizer =decoder_tokenizer
            self.inputs = []
            self.templates=[]
            self.targets = []
            self.skippedcount = 0
            self._build()

        def __len__(self):
            return len(self.inputs)

        def __getitem__(self, index):
            source_ids = self.inputs[index]["input_ids"].squeeze()
            target_ids = self.targets[index]["input_ids"].squeeze()
            src_mask = self.inputs[index]["attention_mask"].squeeze()  # might need to squeeze
            target_mask = self.targets[index]["attention_mask"].squeeze()  # might need to squeeze
            labels = copy.deepcopy(target_ids)
            labels[labels == 0] = -100
            return {"source_ids": source_ids, "source_mask": src_mask,  "target_ids": target_ids, "target_mask": target_mask,
                    "labels": labels}

        def _build(self):
            for inputs, outputs in self.true_false_adjective_tuples:
                input_sent = "summarization: " + inputs[:350]
                ouput_sent = outputs
                tokenized_inputs = self.encoder_tokenizer.batch_encode_plus(
                    [input_sent], max_length=self.max_len_input, pad_to_max_length=True,return_tensors="pt"
                )
                tokenized_targets = self.decoder_tokenizer.batch_encode_plus(
                    [ouput_sent], max_length=self.max_len_output, pad_to_max_length=True,return_tensors="pt"
                )
                self.inputs.append(tokenized_inputs)
                self.targets.append(tokenized_targets)

    train_dataset = FalseGenerationDataset(encoder_tokenizer,decoder_tokenizer,true_false_adjective_tuples_train, max_len_inp, max_len_out)
    validation_dataset = FalseGenerationDataset(encoder_tokenizer,decoder_tokenizer,true_false_adjective_tuples_validation, max_len_inp, max_len_out)
    if nccl:
        sampler = DistributedSampler(train_dataset, num_replicas=dist.get_world_size(), rank=dist.get_rank())
        rewrite_train_dataloader = DataLoader(train_dataset, batch_size=16, sampler=sampler)
        sampler = DistributedSampler(validation_dataset, num_replicas=dist.get_world_size(), rank=dist.get_rank())
        rewrite_val_dataloader = DataLoader(validation_dataset, batch_size=16, sampler=sampler)
    else:
        rewrite_train_dataloader = DataLoader(train_dataset, batch_size=1, shuffle=True)
        rewrite_val_dataloader = DataLoader(validation_dataset, batch_size=1)
    #2.1 binary分类数据集

if classify:
    # config = x.Config()
    np.random.seed(1)
    torch.manual_seed(1)
    torch.cuda.manual_seed_all(1)
    start_time = time.time()
    print("Loading binary data...")
    def binary_dataload(config,data_paths,nccl,batch_size=8):
        train_data, dev_data, test_data = build_dataset(config,data_paths)
        train_sencens=[]
        train_lable=[]
        train_lens=[]
        train_mask=[]
        z=0
        o=0
        for train_s in train_data:
            if train_s[1]==1:
                o+=1
            else:
                z+=1
            train_sencens.append(train_s[0])
            train_lable.append(train_s[1])
            train_lens.append(train_s[2])
            train_mask.append(train_s[3])
        print(z,o,z/(z+o))
        train_dataset = TensorDataset(torch.tensor(train_sencens),torch.tensor(train_lable),torch.tensor(train_lens),torch.tensor(train_mask))
        test_sencens=[]
        test_lable=[]
        test_lens=[]
        test_mask=[]
        z=0
        o=0
        for test_s in test_data:
            if test_s[1]==1:
                o+=1
            else:
                z+=1
            test_sencens.append(test_s[0])
            test_lable.append(test_s[1])
            test_lens.append(test_s[2])
            test_mask.append(test_s[3])
        print(z,o,z/(z+o))
        test_dataset = TensorDataset(torch.tensor(test_sencens), torch.tensor(test_lable), torch.tensor(test_lens),
                                        torch.tensor(test_mask))
        dev_sencens = []
        dev_lable = []
        dev_lens = []
        dev_mask = []
        z=0
        o=0
        for dev_s in dev_data:
            if dev_s[1]==1:
                o+=1
            else:
                z+=1
            dev_sencens.append(dev_s[0])
            dev_lable.append(dev_s[1])
            dev_lens.append(dev_s[2])
            dev_mask.append(dev_s[3])
        print(z,o,z/(z+o))
        dev_dataset = TensorDataset(torch.tensor(dev_sencens), torch.tensor(dev_lable), torch.tensor(dev_lens),
                                        torch.tensor(dev_mask))

        if nccl:
        # 
            sampler_b = DistributedSampler(train_dataset, num_replicas=dist.get_world_size(), rank=dist.get_rank())
            binary_train_iter = DataLoader(train_dataset, batch_size=12, sampler=sampler_b)
            sampler_b = DistributedSampler(dev_dataset, num_replicas=dist.get_world_size(), rank=dist.get_rank())
            binary_dev_iter = DataLoader(dev_dataset, batch_size=12, sampler=sampler_b)
            sampler_b = DistributedSampler(test_dataset, num_replicas=dist.get_world_size(), rank=dist.get_rank())
            binary_test_iter = DataLoader(test_dataset, batch_size=12, sampler=sampler_b)
        else:
            binary_train_iter = DataLoader(train_dataset,sampler=ImbalancedDatasetSampler(train_dataset), batch_size=batch_size)
        # train_iter = DataLoader(train_dataset,shuffle=True, batch_size=config.batch_size)
            binary_dev_iter = DataLoader(dev_dataset, batch_size=batch_size)
            binary_test_iter = DataLoader(test_dataset, batch_size=batch_size)
            return binary_train_iter,binary_dev_iter,binary_test_iter
    data_paths="TCSI_pp/preprocessing/results_new10/class12"
    data_paths_risk="TCSI_pp/preprocessing/results_new10/class13"
    data_paths_sensitive="TCSI_pp/preprocessing/results_new10/class14"
    important_train_iter,important_dev_iter,important_test_iter=binary_dataload(config,data_paths,nccl,batch_size=56)
    risk_train_iter,risk_dev_iter,risk_test_iter=binary_dataload(config,data_paths_risk,nccl,batch_size=32)
    sensitive_train_iter,sensitive_dev_iter,sensitive_test_iter=binary_dataload(config,data_paths_sensitive,nccl,batch_size=32)



    #2.2 multi分类数据集
    # dataset=args.data
    # dataname=args.dataname
    data_multi_paths='TCSI_pp/preprocessing/results_new10/class_multi_a'
    def multi_dataload(config,data_paths,nccl,batch_size=16):
        start_time = time.time()
        print("Loading multi data...")
        # data_paths="../../capp_130/Subdataset/Extraction/topic_identification_dataset"
        train_data, dev_data, test_data = build_dataset_multi(config,data_paths)
        train_sencens=[]
        train_lable=[]
        train_lens=[]
        train_mask=[]
        for train_s in train_data:
            train_sencens.append(train_s[0])
            train_lable.append(train_s[1])
            train_lens.append(train_s[2])
            train_mask.append(train_s[3])
        train_dataset = TensorDataset(torch.tensor(train_sencens),torch.tensor(train_lable),torch.tensor(train_lens),torch.tensor(train_mask))
        test_sencens=[]
        test_lable=[]
        test_lens=[]
        test_mask=[]
        for test_s in test_data:
            test_sencens.append(test_s[0])
            test_lable.append(test_s[1])
            test_lens.append(test_s[2])
            test_mask.append(test_s[3])
        test_dataset = TensorDataset(torch.tensor(test_sencens), torch.tensor(test_lable), torch.tensor(test_lens),
                                        torch.tensor(test_mask))
        dev_sencens = []
        dev_lable = []
        dev_lens = []
        dev_mask = []
        for dev_s in dev_data:
            dev_sencens.append(dev_s[0])
            dev_lable.append(dev_s[1])
            dev_lens.append(dev_s[2])
            dev_mask.append(dev_s[3])
        dev_dataset = TensorDataset(torch.tensor(dev_sencens), torch.tensor(dev_lable), torch.tensor(dev_lens),
                                        torch.tensor(dev_mask))
        # 
        if nccl:
            sampler = DistributedSampler(train_dataset, num_replicas=dist.get_world_size(), rank=dist.get_rank())
            multi_train_iter = DataLoader(train_dataset, batch_size=12, sampler=sampler)
            sampler = DistributedSampler(dev_dataset, num_replicas=dist.get_world_size(), rank=dist.get_rank())
            multi_dev_iter = DataLoader(dev_dataset, batch_size=12, sampler=sampler)
            sampler = DistributedSampler(test_dataset, num_replicas=dist.get_world_size(), rank=dist.get_rank())
            multi_test_iter = DataLoader(test_dataset, batch_size=12, sampler=sampler)
        else:
            multi_train_iter = DataLoader(train_dataset, shuffle=True,batch_size=batch_size)
            multi_dev_iter = DataLoader(dev_dataset, batch_size=batch_size)
            multi_test_iter = DataLoader(test_dataset, batch_size=batch_size)
        return multi_train_iter,multi_dev_iter,multi_test_iter

    multi_train_iter,multi_dev_iter,multi_test_iter=multi_dataload(config,data_multi_paths,nccl,batch_size=16)
# 3. 定义训练参数
if nccl:
# 2. 创建模型并移动到对应的设备
    device = torch.device("cuda", dist.get_rank())
    model.to(device)
    model = DDP(model,find_unused_parameters=True)
else:
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    # model.load_state_dict(torch.load('multi_ddp2_c.ckpt'))
    model.to(device)
    if torch.torch.cuda.device_count() > 1:
        model = DataParallel(model)
    # else:
# else:
#     device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
#     model.to(device)
    
print('-'*10,device,torch.cuda.device_count())
# if torch.cuda.device_count() > 1:
#     model = DataParallel(model)

param_optimizer = list(model.named_parameters())
if rewrite:
    decoder_params = [p for n, p in param_optimizer if 'decoder' in n]
    optimizer_rw = torch.optim.AdamW(decoder_params, lr=lr, weight_decay=1e-5)
    # print(param_optimizer)
    # pdb.set_trace()
if classify:
    no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
    {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay': 0.01},
    {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}]
    num_epochs=100
    optimizer_r = BertAdam(optimizer_grouped_parameters,
                        lr=5e-6,
                        warmup=0.05,
                        t_total=(len(important_train_iter)+len(risk_train_iter)+len(sensitive_train_iter)+len(multi_train_iter)) * 50)
    optimizer_r = torch.optim.AdamW(model.parameters(), lr=lr,weight_decay=1e-5)
# optimizer_b = torch.optim.AdamW(model.parameters(), lr=5e-6)
# optimizer_m = torch.optim.AdamW(model.parameters(), lr=5e-6)
# optimizer_binary = BertAdam(model.parameters(),
#                             lr=5e-6,
#                             warmup=0.05,
#                             t_total=len(train_iter) * config.num_epochs,
#                             weight_decay=0.01)
# 4. 微调模型

if train_step:
    model.train()

    # scaler_r = GradScaler()
    # scaler_b = GradScaler()
    # scaler_m = GradScaler()
    num_epochs=100
    max_dev_f1=0
    min_dev_loss=float('inf')
    if classify:
        for epoch in tqdm(range(50), desc="Epochs"):  # 例如，训练3个epoch
            i_loss=0
            m_loss=0
            risk_loss=0
            s_loss=0
            
            #--------------important train-------------
            pbar_b = tqdm(total=len(important_train_iter), desc='important train batch {:2d}'.format(epoch), leave=False)
            predict_all = np.array([[]], dtype=int)
            labels_all = np.array([[]], dtype=int)
            for batch in important_train_iter:
                model.zero_grad()
                # print(batch)
                loss,pre=model(
                    input_ids=batch[0].to(device),
                    attention_mask=batch[3].to(device),
                    labels=batch[1].to(device),
                    task='important'
                )
                # print('loss',loss)
                loss = torch.mean(loss)
                # scaler_r.scale(loss).backward()
                i_loss+=loss
                loss.backward()
                optimizer_r.step()
                # scaler_r.step(optimizer_r)
                # scaler_r.update()
                pbar_b.update(1)
                pbar_b.set_postfix(loss='{:.3f}'.format(loss.item()))
                predic = pre.data.argmax(axis=1)
                labels_all = np.append(labels_all, batch[1].cpu().numpy())
                predict_all = np.append(predict_all, predic.cpu().numpy())
            # 关闭进度条
            pbar_b.close()
            torch.cuda.empty_cache()
            f1 = f1_score(labels_all, predict_all,average=None)
            print("----important train f1----:",f1)
            micro_important_f1 = f1_score(labels_all, predict_all, average='micro')
            macro_important_f1 = f1_score(labels_all, predict_all, average='macro')
            print(f"important train Micro-F1: {micro_important_f1}")
            print(f"important train Macro-F1: {macro_important_f1}")
            print('important train_loss:',i_loss/len(important_train_iter))
            
            
            #---------multi train----------
            pbar_m = tqdm(total=len(multi_train_iter), desc='multi batch {:2d}'.format(epoch), leave=False)
            predict_all = np.array([[]], dtype=int)
            labels_all = np.array([[]], dtype=int)
            for batch in multi_train_iter:
                model.zero_grad()
                loss,pre=model(
                    input_ids=batch[0].to(device),
                    attention_mask=batch[3].to(device),
                    labels=batch[1].to(device),
                    task='multi'
                )
                loss = torch.mean(loss)
                # scaler_r.scale(loss).backward()
                m_loss+=loss
                loss.backward()
                optimizer_r.step()
                # scaler_r.step(optimizer_r)
                # scaler_r.update()
                pbar_m.update(1)
                pbar_m.set_postfix(loss='{:.3f}'.format(loss.item()))
                loss = torch.mean(loss)
                m=torch.nn.Sigmoid()
                predic = m(pre.data)
                Threshold = 0.5
                predic[predic > Threshold] = 1
                predic[predic <= Threshold] = 0
                if labels_all.shape[1] == 0:
                    # print('6789')
                    labels_all=batch[1].cpu().numpy()
                    predict_all=predic.cpu().numpy()
                else:
                    labels=batch[1].cpu().numpy()
                    predic = predic.cpu().numpy()
                    labels_all = np.vstack((labels_all, labels))
                    predict_all = np.vstack((predict_all, predic))
                # print(labels_all.shape,predict_all.shape)
            f1 = f1_score(labels_all, predict_all,average=None)
            print("----multi train f1----:",f1)
            micro_f1 = f1_score(labels_all, predict_all, average='micro')
            macro_multi_f1 = f1_score(labels_all, predict_all, average='macro')
            print(f"multi train Micro-F1: {micro_f1}")
            print(f"multi train Macro-F1: {macro_multi_f1}")
            pbar_m.close()
            torch.cuda.empty_cache()
            print('multi train_loss:',m_loss/len(multi_train_iter))
            
            #-----------risk train-------------
            pbar_b = tqdm(total=len(risk_train_iter), desc='risk train batch {:2d}'.format(epoch), leave=False)
            predict_all = np.array([[]], dtype=int)
            labels_all = np.array([[]], dtype=int)
            for batch in risk_train_iter:
                model.zero_grad()
                loss,pre=model(
                    input_ids=batch[0].to(device),
                    attention_mask=batch[3].to(device),
                    labels=batch[1].to(device),
                    task='risk'
                )
                loss = torch.mean(loss)
                # scaler_r.scale(loss).backward()
                risk_loss+=loss
                loss.backward()
                optimizer_r.step()
                # scaler_r.step(optimizer_r)
                # scaler_r.update()
                pbar_b.update(1)
                pbar_b.set_postfix(loss='{:.3f}'.format(loss.item()))
                predic = pre.data.argmax(axis=1)
                labels_all = np.append(labels_all, batch[1].cpu().numpy())
                predict_all = np.append(predict_all, predic.cpu().numpy())
            pbar_b.close()
            torch.cuda.empty_cache()
            f1 = f1_score(labels_all, predict_all,average=None)
            print("----risk train f1----:",f1)
            micro_risk_f1 = f1_score(labels_all, predict_all, average='micro')
            macro_risk_f1 = f1_score(labels_all, predict_all, average='macro')
            print(f"risk train Micro-F1: {micro_risk_f1}")
            print(f"risk train Macro-F1: {macro_risk_f1}")
            print('risk train_loss:',risk_loss/len(risk_train_iter))
            
            
            #-------------sensitive train---------------
            pbar_b = tqdm(total=len(sensitive_train_iter), desc='sensitive train batch {:2d}'.format(epoch), leave=False)
            predict_all = np.array([[]], dtype=int)
            labels_all = np.array([[]], dtype=int)
            for batch in sensitive_train_iter:
                model.zero_grad()
                # print(batch)
                loss,pre=model(
                    input_ids=batch[0].to(device),
                    attention_mask=batch[3].to(device),
                    labels=batch[1].to(device),
                    task='sensitive'
                )
                loss = torch.mean(loss)
                # scaler_r.scale(loss).backward()
                s_loss+=loss
                loss.backward()
                optimizer_r.step()
                # scaler_r.step(optimizer_r)
                # scaler_r.update()
                pbar_b.update(1)
                pbar_b.set_postfix(loss='{:.3f}'.format(loss.item()))
                predic = pre.data.argmax(axis=1)
                labels_all = np.append(labels_all, batch[1].cpu().numpy())
                predict_all = np.append(predict_all, predic.cpu().numpy())
            pbar_b.close()
            torch.cuda.empty_cache()
            f1 = f1_score(labels_all, predict_all,average=None)
            print("----sensitive train f1----:",f1)
            micro_f1 = f1_score(labels_all, predict_all, average='micro')
            macro_sensitive_f1 = f1_score(labels_all, predict_all, average='macro')
            print(f"sensitive train Micro-F1: {micro_f1}")
            print(f"sensitive train Macro-F1: {macro_sensitive_f1}")
            print('sensitive train_loss:',s_loss/len(sensitive_train_iter))
            
            #---------------------------
            #验证
            if epoch%1==0:
                with torch.no_grad(): 
                    iv_loss=0
                    mv_loss=0
                    riv_loss=0
                    sv_loss=0
                    #---------import dev-----------
                    pbar_bv = tqdm(total=len(important_dev_iter), desc='important dev {:2d}'.format(epoch), leave=False)
                    predict_all = np.array([[]], dtype=int)
                    labels_all = np.array([[]], dtype=int)
                    for batch in important_dev_iter:
                        loss,pre=model(
                            input_ids=batch[0].to(device),
                            attention_mask=batch[3].to(device),
                            labels=batch[1].to(device),
                            task='important'
                        )
                        loss = torch.mean(loss)
                        iv_loss+=loss
                        predic = pre.data.argmax(axis=1)
                        labels_all = np.append(labels_all, batch[1].cpu().numpy())
                        predict_all = np.append(predict_all, predic.cpu().numpy())
                        f1 = f1_score(np.array(batch[1].clone().detach().cpu()), np.array(predic.clone().detach().cpu()),average=None)
                        pbar_bv.update(1)
                        pbar_bv.set_postfix(loss='{:.3f}'.format(loss.item()))
                    f1 = f1_score(labels_all, predict_all,average=None)
                    print("----important val f1----:",f1)
                    micro_important_f1 = f1_score(labels_all, predict_all, average='micro')
                    macro_important_f1 = f1_score(labels_all, predict_all, average='macro')
                    print(f"important val Micro-F1: {micro_important_f1}")
                    print(f"important val Macro-F1: {macro_important_f1}")
                    pbar_bv.close()
                    torch.cuda.empty_cache()
                    print('important val_loss:',iv_loss/len(important_dev_iter))
                    

                    
                    #--------------multi dev-------------
                    pbar_mv = tqdm(total=len(multi_dev_iter), desc='multi dev {:2d}'.format(epoch), leave=False)
                    predict_all = np.array([[]], dtype=int)
                    labels_all = np.array([[]], dtype=int)
                    for batch in multi_dev_iter:
                        loss,pre =model(
                            input_ids=batch[0].to(device),
                            attention_mask=batch[3].to(device),
                            labels=batch[1].to(device),
                            task='multi'
                        )
                        loss = torch.mean(loss)
                        m_loss+=loss
                        m=torch.nn.Sigmoid()
                        predic = m(pre.data)
                        Threshold = 0.5
                        predic[predic > Threshold] = 1
                        predic[predic <= Threshold] = 0
                        if labels_all.shape[1] == 0:
                            # print('6789')
                            labels_all=batch[1].cpu().numpy()
                            predict_all=predic.cpu().numpy()
                        else:
                            labels=batch[1].cpu().numpy()
                            predic = predic.cpu().numpy()
                            labels_all = np.vstack((labels_all, labels))
                            predict_all = np.vstack((predict_all, predic))
                        # print(labels_all.shape,predict_all.shape)
                        pbar_mv.update(1)
                        pbar_mv.set_postfix(loss='{:.3f}'.format(loss.item()))

                    f1 = f1_score(labels_all, predict_all,average=None)
                    print("----multi dev f1----:",f1)
                    micro_f1 = f1_score(labels_all, predict_all, average='micro')
                    macro_multi_f1 = f1_score(labels_all, predict_all, average='macro')
                    print(f"multi dev Micro-F1: {micro_f1}")
                    print(f"multi dev Macro-F1: {macro_multi_f1}")
                    pbar_mv.close()
                    torch.cuda.empty_cache()
                    print('multi dev_loss:',m_loss/len(multi_dev_iter))




                    #----------------risk dev----------------
                    pbar_bv = tqdm(total=len(risk_dev_iter), desc='risk dev {:2d}'.format(epoch), leave=False)
                    predict_all = np.array([[]], dtype=int)
                    labels_all = np.array([[]], dtype=int)
                    for batch in risk_dev_iter:
                        loss,pre=model(
                            input_ids=batch[0].to(device),
                            attention_mask=batch[3].to(device),
                            labels=batch[1].to(device),
                            task='risk'
                        )
                        loss = torch.mean(loss)
                        riv_loss+=loss
                        predic = pre.data.argmax(axis=1)
                        labels_all = np.append(labels_all, batch[1].cpu().numpy())
                        predict_all = np.append(predict_all, predic.cpu().numpy())
                        pbar_bv.update(1)
                        pbar_bv.set_postfix(loss='{:.3f}'.format(loss.item()))
                    f1 = f1_score(labels_all, predict_all,average=None)
                    print("----risk val f1----:",f1)
                    micro_risk_f1 = f1_score(labels_all, predict_all, average='micro')
                    macro_risk_f1 = f1_score(labels_all, predict_all, average='macro')
                    print(f"risk val Micro-F1: {micro_risk_f1}")
                    print(f"risk val Macro-F1: {macro_risk_f1}")
                    pbar_bv.close()
                    torch.cuda.empty_cache()
                    print('risk val_loss:',riv_loss/len(risk_dev_iter))
                    

                    #-------------------sensitive dev----------
                    pbar_bv = tqdm(total=len(sensitive_dev_iter), desc='sensitive dev {:2d}'.format(epoch), leave=False)
                    predict_all = np.array([[]], dtype=int)
                    labels_all = np.array([[]], dtype=int)
                    for batch in sensitive_dev_iter:
                        loss,pre=model(
                            input_ids=batch[0].to(device),
                            attention_mask=batch[3].to(device),
                            labels=batch[1].to(device),
                            task='sensitive'
                        )
                        loss = torch.mean(loss)
                        sv_loss+=loss
                        predic = pre.data.argmax(axis=1)
                        labels_all = np.append(labels_all, batch[1].cpu().numpy())
                        predict_all = np.append(predict_all, predic.cpu().numpy())
                        pbar_bv.update(1)
                        pbar_bv.set_postfix(loss='{:.3f}'.format(loss.item()))
                    f1 = f1_score(labels_all, predict_all,average=None)
                    print("----sensitive val f1----:",f1)
                    micro_f1 = f1_score(labels_all, predict_all, average='micro')
                    macro_sensitive_f1 = f1_score(labels_all, predict_all, average='macro')
                    print(f"sensitive val Micro-F1: {micro_f1}")
                    print(f"sensitive val Macro-F1: {macro_sensitive_f1}")
                    pbar_bv.close()
                    torch.cuda.empty_cache()
                    print('sensitive val_loss:',sv_loss/len(sensitive_dev_iter))
            model.train()
                    
            #----------save model----------
            mean_f1=np.mean([macro_multi_f1,macro_risk_f1,macro_important_f1,macro_sensitive_f1])
            if mean_f1>max_dev_f1:
                torch.save(model.state_dict(), 'test_ddp2_cw1.ckpt')
                print(f'epoch{epoch}分类f1性能提升:',mean_f1)
                max_dev_f1=mean_f1
    if rewrite:    
        model.load_state_dict(torch.load('multi_xlnet2gpt_ddp2_cw.ckpt'))

        for epoch in tqdm(range(100), desc="Epochs"):  # 例如，训练3个epoch
            r_loss=0
            pbar_r = tqdm(total=len(rewrite_train_dataloader), desc='rewrite batch {:2d}'.format(epoch), leave=False)
            for batch in rewrite_train_dataloader:
                model.zero_grad()
                # batch.to(device)
                # inputs = {key: val.to(device) for key, val in batch.items()}
                loss=model(
                    input_ids=batch["source_ids"].to(device),
                    attention_mask=batch["source_mask"].to(device),
                    labels=batch['labels'].to(device),
                    decoder_input_ids=batch["target_ids"].to(device),
                    decoder_attention_mask=batch['target_mask'].to(device),
                    task='rewrite'
                )
                # print('loss',loss)
                # print('预测：',outputs.device,outputs.shape)
                loss = torch.mean(loss)
                # loss = outputs.loss
                r_loss+=loss
                loss.backward()
                optimizer_rw.step()
                # scaler_r.scale(loss).backward()
                # scaler_r.step(optimizer_r)
                # scaler_r.update()
                pbar_r.update(1)
                pbar_r.set_postfix(loss='{:.3f}'.format(loss.item()))
                pbar_r.close()
            torch.cuda.empty_cache()
            mean_loss=r_loss/len(rewrite_train_dataloader)
            perplexity = torch.exp(mean_loss).item()
            print(f"rewrite train Perplexity: {perplexity}")
            print('rewrite train_loss:',mean_loss)
            if epoch%1==0:
                rv_loss=0
                with torch.no_grad(): 
                    pbar_rv = tqdm(total=len(rewrite_val_dataloader), desc='rewrite batch {:2d}'.format(epoch), leave=False)
                    for batch in rewrite_val_dataloader:
                        loss =model(
                            input_ids=batch["source_ids"].to(device),
                            attention_mask=batch["source_mask"].to(device),
                            labels=batch['labels'].to(device),
                            decoder_input_ids=batch["target_ids"].to(device),
                            decoder_attention_mask=batch['target_mask'].to(device),
                            task='rewrite'
                        )
                        loss = torch.mean(loss)
                        rv_loss+=loss
                        pbar_rv.update(1)
                        pbar_rv.set_postfix(loss='{:.3f}'.format(loss.item()))
                    pbar_rv.close()
                    torch.cuda.empty_cache()
                    mean_loss=rv_loss/len(rewrite_val_dataloader)
                    perplexity = torch.exp(mean_loss).item()
                    print(f"rewrite dev Perplexity: {perplexity}")
                    print('rewrite dev_loss:',mean_loss)
            if  mean_loss<min_dev_loss:
                torch.save(model.state_dict(), 'multi_xlnet2gpt_ddp2_rw1.ckpt')
                print(f'epoch{epoch}改写loss下降:',mean_loss)
                min_dev_loss=mean_loss


if test_step:
    model.load_state_dict(torch.load('multi_mt5_ddp2_rw1.ckpt'))
    # print(model)
    model.eval()
    # 禁用梯度计算
    with torch.no_grad():
        with open(save_rewrite_path, 'w', encoding='utf-8') as fp:
            for text in true_false_adjective_tuples_test:
                test_tokenized = encoder_tokenizer.encode_plus('summarization'+text[0][:250], return_tensors="pt")
                test_input_ids = test_tokenized["input_ids"].to(device)
                test_attention_mask = test_tokenized["attention_mask"].to(device)
                if torch.torch.cuda.device_count() > 1:
                    beam_outputs = model.module.model.generate(
                            input_ids=test_input_ids,
                            attention_mask=test_attention_mask,
                            # encoder_hidden_states=encoder_outputs.last_hidden_state,
                            # encoder_attention_mask=test_attention_mask,
                            # decoder_start_token_id=model.module.config.decoder_start_token_id,
                            max_length=150,
                            early_stopping=True,
                            num_beams=10,
                            num_return_sequences=1,
                            no_repeat_ngram_size=2,
                        )
                else:
                    beam_outputs = model.model.generate(
                            input_ids=test_input_ids,
                            attention_mask=test_attention_mask,
                            # encoder_hidden_states=encoder_outputs.last_hidden_state,
                            # encoder_attention_mask=test_attention_mask,
                            # decoder_start_token_id=model.module.config.decoder_start_token_id,
                            max_length=150,
                            early_stopping=True,
                            num_beams=10,
                            num_return_sequences=1,
                            no_repeat_ngram_size=2,
                        )
                for beam_output in beam_outputs:
                    sent = decoder_tokenizer.decode(beam_output, skip_special_tokens=True, clean_up_tokenization_spaces=True)
                sent=''.join(sent.split())
                fp.write(json.dumps({'text': text[0], 'pred': sent, 'rewrite': text[1]},ensure_ascii=False) + '\n')
print(rouge_scorces(save_rewrite_path))
                # encoder_outputs=model.model.encoder(test_input_ids, attention_mask=test_attention_mask)
            # for batch in multi_train_iter:
        # print(batch)
                # loss=model(
                #     encoder_outputs=encoder_outputs,
                #     task='multi'
                # )
                # loss=model(
                #     input_ids=test_input_ids,
                #     attention_mask=test_attention_mask,
                #     task='generate'
                # )
                # pdb.set_trace()
                # output = model.model.generate(input_ids=test_input_ids,attention_mask=test_attention_mask)
                # output = model.decoder.generate(input_ids=test_input_ids,encoder_outputs=encoder_outputs)
                # print('一个一个又一个')
            # print('loss',loss)
            # 使用 torch.no_grad() 包装 generate 调用
                
            
             
    #     beam_outputs = model.generate(
    #                 input_ids=test_input_ids,
    #                 attention_mask=test_attention_mask,
    #                 max_length=250,
    #                 early_stopping=True,
    #                 num_beams=10,
    #                 num_return_sequences=1,
    #                 no_repeat_ngram_size=2,
    #             )
        